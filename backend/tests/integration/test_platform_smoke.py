from __future__ import annotations

from typing import cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from httpx import AsyncClient

from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.events.dispatcher import command_resource_id
from wechat_bot.outbox.sender import SenderClientFactory, SenderOptions, SenderWorker

BOOTSTRAP_TOKEN = "test-bootstrap-token-with-at-least-32-characters"
OWNER_USERNAME = "platform-owner"
OWNER_PASSWORD = "platform owner password 123"
GEWE_TOKEN = "platform-smoke-token-must-never-be-returned"
BOT_APP_ID = "app-platform-smoke"
BOT_WXID = "wxid_platform_bot"
MEMBER_WXID = "wxid_platform_member"
CHATROOM_WXID = "platform-smoke-room@chatroom"
DISCOVERY_MESSAGE_ID = "9007199254741101"
COMMAND_MESSAGE_ID = "9007199254741102"


class RejectingGeWeClientFactory:
    """Proves that a revoked message is rejected before any GeWe client is opened."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> object:
        del base_url, token, timeout_seconds
        self.calls += 1
        raise AssertionError("revoked outbox message attempted to contact GeWe")


async def test_platform_management_to_fail_closed_outbox_smoke(
    app: FastAPI,
    client: AsyncClient,
    settings: Settings,
) -> None:
    bootstrap = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={
            "username": OWNER_USERNAME,
            "display_name": "Platform Owner",
            "password": OWNER_PASSWORD,
        },
    )
    assert bootstrap.status_code == 201
    assert bootstrap.json()["roles"] == ["owner"]

    csrf = await client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    csrf_token = csrf.json()["csrf_token"]
    missing_csrf = await client.post(
        "/api/auth/login",
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert missing_csrf.status_code == 403
    login = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["user"]["roles"] == ["owner"]
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert (await client.get("/api/auth/me")).status_code == 200

    connection = await client.post(
        "/api/v1/connections",
        json={
            "workspace_slug": "platform-smoke",
            "workspace_name": "Platform Smoke",
            "name": "Smoke GeWe",
            "api_base_url": "https://api.gewe.test",
            "token": GEWE_TOKEN,
        },
    )
    assert connection.status_code == 201
    connection_body = connection.json()
    assert "token" not in connection_body
    assert GEWE_TOKEN not in connection.text
    workspace_id = connection_body["workspace_id"]
    callback_path = urlsplit(connection_body["callback_url"]).path

    account = await client.post(
        f"/api/v1/connections/{connection_body['id']}/bot-accounts",
        json={"app_id": BOT_APP_ID, "wxid": BOT_WXID, "note": "smoke fixture"},
    )
    assert account.status_code == 201
    assert account.json()["status"] == "OFFLINE"
    account_id = account.json()["id"]

    installed = await client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": workspace_id},
    )
    assert installed.status_code == 201
    created = await client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": installed.json()["plugin"]["id"],
            "package_version_id": installed.json()["package"]["id"],
            "name": "Platform Echo",
            "config": {"prefix": "smoke:"},
            "scope": {"workspace_id": workspace_id},
            "grants": ["message.reply.text"],
        },
    )
    assert created.status_code == 201
    deployment_id = created.json()["deployment"]["id"]
    revision_id = created.json()["revision"]["id"]
    activated = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{revision_id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["deployment"]["status"] == "RUNNING"

    discovery_payload = _text_callback(
        message_id=DISCOVERY_MESSAGE_ID,
        text="hello before group authorization",
    )
    discovery = await client.post(callback_path, json=discovery_payload)
    assert discovery.status_code == 200
    assert await app.state.event_dispatcher_worker.run_once() == 1

    chatrooms = await client.get(f"/api/v1/directory/bot-accounts/{account_id}/chatrooms")
    assert chatrooms.status_code == 200
    assert chatrooms.json()["total"] == 1
    chatroom = chatrooms.json()["items"][0]
    assert chatroom["chatroom_id"] == CHATROOM_WXID
    assert chatroom["discovered_from"] == "WEBHOOK"

    rule = await client.post(
        "/api/v1/policy/rules",
        json={
            "workspace_id": workspace_id,
            "scope_type": "CHATROOM",
            "scope_id": chatroom["id"],
            "resource_type": "COMMAND",
            "resource_id": command_resource_id("builtin.echo", "echo"),
            "effect": "ALLOW",
            "reason": "allow Echo in the smoke-test group",
        },
    )
    assert rule.status_code == 201

    command_payload = _text_callback(
        message_id=COMMAND_MESSAGE_ID,
        text="/echo hello",
    )
    first_callback = await client.post(callback_path, json=command_payload)
    duplicate_callback = await client.post(callback_path, json=command_payload)
    assert first_callback.status_code == 200
    assert duplicate_callback.status_code == 200
    assert first_callback.text == duplicate_callback.text == ""
    assert await app.state.event_dispatcher_worker.run_once() == 1
    assert await app.state.event_dispatcher_worker.run_once() == 0

    outbox = await client.get("/api/v1/outbox")
    assert outbox.status_code == 200
    assert outbox.json()["total"] == 1
    outbox_message = outbox.json()["items"][0]
    assert outbox_message["status"] == "PENDING"
    assert outbox_message["target_wxid"] == CHATROOM_WXID
    assert outbox_message["payload"] == {"text": "smoke:hello", "at_wxids": []}
    assert "authorization_context" not in outbox.text

    messages = await client.get(
        "/api/v1/messages",
        params={
            "bot_account_id": account_id,
            "conversation_type": "GROUP",
            "conversation_id": CHATROOM_WXID,
        },
    )
    assert messages.status_code == 200
    assert messages.json()["total"] == 2
    command_message = next(
        item
        for item in messages.json()["items"]
        if item["provider_message_id"] == COMMAND_MESSAGE_ID
    )
    assert command_message["inbox_status"] == "DISPATCHED"

    message_detail = await client.get(f"/api/v1/messages/{command_message['id']}")
    assert message_detail.status_code == 200
    assert message_detail.json()["raw_payload"] == command_payload
    assert GEWE_TOKEN not in message_detail.text

    trace_id = command_message["trace_id"]
    trace = await client.get(f"/api/v1/traces/{trace_id}")
    assert trace.status_code == 200
    trace_body = trace.json()
    assert trace_body["message"]["id"] == command_message["id"]
    assert [item["effect"] for item in trace_body["policy_decisions"]] == ["ALLOW"]
    assert any(item["result"] == "SUCCEEDED" for item in trace_body["audit_events"])
    assert [item["id"] for item in trace_body["outbox_messages"]] == [outbox_message["id"]]
    assert "authorization_context" not in trace.text
    assert GEWE_TOKEN not in trace.text

    revoked = await client.post(f"/api/v1/policy/rules/{rule.json()['id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    rejecting_factory = RejectingGeWeClientFactory()
    sender = SenderWorker(
        session_factory=app.state.database.session_factory,
        cipher=CredentialCipher.from_settings(settings),
        options=SenderOptions(
            poll_interval_seconds=0.01,
            per_minute_limit=100,
            target_interval_seconds=0,
            group_interval_min_seconds=0,
            group_interval_max_seconds=0,
            retry_jitter_ratio=0,
            lease_seconds=61,
            request_timeout_seconds=1,
        ),
        client_factory=cast(SenderClientFactory, rejecting_factory),
    )
    assert await sender.run_once() == 1
    assert rejecting_factory.calls == 0

    cancelled = await client.get(f"/api/v1/outbox/{outbox_message['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["last_error_code"] == "POLICY_CHANGED"
    assert cancelled.json()["attempt_count"] == 0

    updated_trace = await client.get(f"/api/v1/traces/{trace_id}")
    assert [item["effect"] for item in updated_trace.json()["policy_decisions"]] == [
        "ALLOW",
        "DENY",
    ]
    assert updated_trace.json()["outbox_messages"][0]["status"] == "CANCELLED"
    assert updated_trace.json()["outbox_messages"][0]["last_error_code"] == "POLICY_CHANGED"
    assert GEWE_TOKEN not in "".join(
        (
            connection.text,
            account.text,
            installed.text,
            created.text,
            activated.text,
            outbox.text,
            messages.text,
            trace.text,
            cancelled.text,
            updated_trace.text,
        )
    )


def _text_callback(*, message_id: str, text: str) -> dict[str, object]:
    return {
        "appid": BOT_APP_ID,
        "wxid": BOT_WXID,
        "content": f"{MEMBER_WXID}:\n{text}",
        "createTime": 1_725_000_000,
        "fromUser": CHATROOM_WXID,
        "isSelf": False,
        "msgType": "TEXT",
        "newMsgId": message_id,
        "toUser": BOT_WXID,
    }
