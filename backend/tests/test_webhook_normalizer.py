from wechat_bot.db.models import ConversationType
from wechat_bot.webhooks.normalizer import normalize_gewe_payload


def test_v2_group_message_preserves_large_id_as_string() -> None:
    envelope = normalize_gewe_payload(
        {
            "appid": "app-1",
            "wxid": "wxid_bot",
            "content": "wxid_member:\n你好",
            "createTime": 1_725_000_000,
            "fromUser": "123@chatroom",
            "isSelf": False,
            "msgType": "TEXT",
            "newMsgId": 9_154_000_000_000_000_001,
            "toUser": "wxid_bot",
        }
    )

    assert envelope.schema_version == "v2"
    assert envelope.event_type == "gewe.message.text"
    assert envelope.new_msg_id == "9154000000000000001"
    assert envelope.conversation_type is ConversationType.GROUP
    assert envelope.conversation_id == "123@chatroom"
    assert envelope.actor_wxid == "wxid_member"


def test_v1_self_message_is_detected() -> None:
    envelope = normalize_gewe_payload(
        {
            "TypeName": "AddMsg",
            "Appid": "app-1",
            "Wxid": "wxid_bot",
            "Data": {
                "NewMsgId": 123,
                "MsgType": 1,
                "FromUserName": {"string": "wxid_bot"},
                "ToUserName": {"string": "wxid_friend"},
                "Content": {"string": "hello"},
                "CreateTime": 1_725_000_000,
            },
        }
    )

    assert envelope.schema_version == "v1"
    assert envelope.event_type == "gewe.message.text"
    assert envelope.is_self is True
    assert envelope.conversation_id == "wxid_friend"


def test_unknown_callback_is_recorded_as_verification() -> None:
    envelope = normalize_gewe_payload({"test": "验证回调地址是否可用"})

    assert envelope.schema_version == "unknown"
    assert envelope.event_type == "gewe.callback_verification"
