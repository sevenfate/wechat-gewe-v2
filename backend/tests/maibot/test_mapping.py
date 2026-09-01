from __future__ import annotations

import pytest

from wechat_bot.maibot.constants import MAIBOT_API_KEY_PLACEHOLDER
from wechat_bot.maibot.mapping import (
    MaiBotInboundText,
    MaiBotProtocolError,
    build_inbound_text_envelope,
    materialize_api_key,
    parse_outbound_text_envelope,
)


def test_group_inbound_keeps_transport_and_business_ids_separate() -> None:
    envelope = build_inbound_text_envelope(
        MaiBotInboundText(
            envelope_id="transport-1",
            business_message_id="gewe:app-1:9007199254740993",
            timestamp=1_788_055_200.25,
            actor_id="wxid_member",
            actor_nickname="Alice",
            actor_cardname="群名片",
            group_id="123@chatroom",
            group_name="测试群",
            text="普通聊天，不需要 @",  # noqa: RUF001
            connector_context_id="context-1",
            platform_account_id="app-1",
            platform_scope="deployment-1",
        )
    )

    assert envelope["msg_id"] == "transport-1"
    payload = envelope["payload"]
    assert payload["message_info"]["message_id"] == "gewe:app-1:9007199254740993"
    sender = payload["message_info"]["sender_info"]
    assert sender["user_info"]["user_id"] == "wxid_member"
    assert sender["group_info"]["group_id"] == "123@chatroom"
    assert "receiver_info" not in payload["message_info"]
    assert payload["message_segment"] == {
        "type": "seglist",
        "data": [{"type": "text", "data": "普通聊天，不需要 @"}],  # noqa: RUF001
    }
    assert envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
    assert payload["message_dim"]["api_key"] == MAIBOT_API_KEY_PLACEHOLDER


def test_private_inbound_contains_only_sender_user_identity() -> None:
    envelope = build_inbound_text_envelope(
        MaiBotInboundText(
            envelope_id="transport-private",
            business_message_id="gewe:app-1:42",
            timestamp=1_788_055_200.25,
            actor_id="wxid_contact",
            text="你好",
            connector_context_id="context-private",
            platform_account_id="app-1",
            platform_scope="deployment-1",
        )
    )

    sender = envelope["payload"]["message_info"]["sender_info"]
    assert sender == {"user_info": {"platform": "gewe", "user_id": "wxid_contact"}}


def test_outbound_target_comes_from_receiver_and_reply_precedes_text() -> None:
    intent = parse_outbound_text_envelope(
        _outbound(
            receiver={
                "group_info": {
                    "platform": "gewe",
                    "group_id": "123@chatroom",
                    "group_name": "display only",
                }
            },
            segments=[
                {"type": "reply", "data": "gewe:app-1:42"},
                {"type": "text", "data": "第一段"},
                {"type": "text", "data": "第二段"},
            ],
        )
    )

    assert intent.target_wxid == "123@chatroom"
    assert intent.target_kind == "GROUP"
    assert intent.reply_to_business_message_id == "gewe:app-1:42"
    assert intent.text == "第一段第二段"


def test_outbound_rejects_sender_identity_as_a_delivery_target() -> None:
    envelope = _outbound(
        receiver={},
        segments=[{"type": "text", "data": "不能发送"}],
    )
    envelope["payload"]["message_info"]["sender_info"] = {
        "user_info": {
            "platform": "gewe",
            "user_id": "attacker-controlled-display-id",
        }
    }

    with pytest.raises(MaiBotProtocolError, match="receiver_info"):
        parse_outbound_text_envelope(envelope)


def test_outbound_rejects_unsupported_media_and_ambiguous_target() -> None:
    with pytest.raises(MaiBotProtocolError, match="unsupported outbound segment"):
        parse_outbound_text_envelope(
            _outbound(
                receiver={"user_info": {"platform": "gewe", "user_id": "wxid_contact"}},
                segments=[{"type": "image", "data": "base64"}],
            )
        )
    with pytest.raises(MaiBotProtocolError, match="exactly one target"):
        parse_outbound_text_envelope(
            _outbound(
                receiver={
                    "group_info": {"platform": "gewe", "group_id": "123@chatroom"},
                    "user_info": {"platform": "gewe", "user_id": "wxid_contact"},
                },
                segments=[{"type": "text", "data": "ambiguous"}],
            )
        )


def test_outbound_accepts_maibot_group_target_with_bot_user_metadata() -> None:
    intent = parse_outbound_text_envelope(
        _outbound(
            receiver={
                "group_info": {"platform": "gewe", "group_id": "123@chatroom"},
                "user_info": {"platform": "gewe", "user_id": "app-1"},
            },
            segments=[{"type": "text", "data": "群消息"}],
            api_key="secret",
        ),
        expected_api_key="secret",
        allow_group_with_user=True,
    )

    assert intent.target_wxid == "123@chatroom"
    assert intent.target_kind == "GROUP"


def test_materialize_api_key_does_not_mutate_persisted_envelope() -> None:
    envelope = _outbound(
        receiver={"user_info": {"platform": "gewe", "user_id": "wxid_contact"}},
        segments=[{"type": "text", "data": "hello"}],
    )
    envelope["meta"]["sender_user"] = MAIBOT_API_KEY_PLACEHOLDER
    envelope["payload"]["message_dim"]["api_key"] = MAIBOT_API_KEY_PLACEHOLDER

    materialized = materialize_api_key(envelope, "real-secret-key")

    assert materialized["meta"]["sender_user"] == "real-secret-key"
    assert materialized["payload"]["message_dim"]["api_key"] == "real-secret-key"
    assert envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
    assert envelope["payload"]["message_dim"]["api_key"] == MAIBOT_API_KEY_PLACEHOLDER


def _outbound(
    *,
    receiver: dict[str, object],
    segments: list[dict[str, str]],
    api_key: str = "secret",
) -> dict[str, object]:
    return {
        "ver": 1,
        "msg_id": "maibot-envelope-1",
        "type": "sys_std",
        "meta": {
            "sender_user": api_key,
            "platform": "gewe",
            "timestamp": 1_788_055_200.25,
        },
        "payload": {
            "message_info": {
                "platform": "gewe",
                "message_id": "maibot-business-1",
                "time": 1_788_055_200.25,
                "receiver_info": receiver,
            },
            "message_segment": {"type": "seglist", "data": segments},
            "message_dim": {"api_key": api_key, "platform": "gewe"},
        },
    }
