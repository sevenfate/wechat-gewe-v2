from __future__ import annotations

import hashlib
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.db.models import AuditEvent, OutboxMessage, WebhookInbox
from wechat_bot.db.policy_models import AclEffect, PolicyDecision


async def _seed_message(admin_client: AsyncClient) -> tuple[str, str]:
    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Observability",
            "api_base_url": "https://api.gewe.test",
            "token": "observability-token",
        },
    )
    account = await admin_client.post(
        f"/api/v1/connections/{connection.json()['id']}/bot-accounts",
        json={"app_id": "app-observability", "wxid": "wxid_bot"},
    )
    callback = await admin_client.post(
        urlsplit(connection.json()["callback_url"]).path,
        json={
            "appid": "app-observability",
            "wxid": "wxid_bot",
            "content": "wxid_member:\nhello trace",
            "createTime": 1_725_000_000,
            "fromUser": "trace-room@chatroom",
            "isSelf": False,
            "msgType": "TEXT",
            "newMsgId": "9007199254740999",
            "toUser": "wxid_bot",
        },
    )
    assert callback.status_code == 200
    return account.json()["id"], connection.json()["workspace_id"]


async def test_message_list_detail_and_filters(
    admin_client: AsyncClient,
) -> None:
    account_id, _ = await _seed_message(admin_client)

    result = await admin_client.get(
        "/api/v1/messages",
        params={"bot_account_id": account_id, "conversation_type": "GROUP"},
    )

    assert result.status_code == 200
    assert result.json()["total"] == 1
    summary = result.json()["items"][0]
    assert summary["text_preview"] == "wxid_member:\nhello trace"
    assert summary["conversation_id"] == "trace-room@chatroom"
    assert summary["actor_wxid"] == "wxid_member"
    assert summary["provider_message_id"] == "9007199254740999"

    detail = await admin_client.get(f"/api/v1/messages/{summary['id']}")
    assert detail.status_code == 200
    assert detail.json()["raw_payload"]["content"].endswith("hello trace")
    assert detail.json()["payload_sha256"]

    empty = await admin_client.get(
        "/api/v1/messages",
        params={"conversation_id": "missing@chatroom"},
    )
    assert empty.json() == {"items": [], "total": 0}


async def test_trace_combines_policy_audit_and_outbox_without_private_context(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    account_id, workspace_id = await _seed_message(admin_client)
    database = app.state.database
    async with database.session_factory() as session, session.begin():
        inbox = await session.scalar(select(WebhookInbox))
        assert inbox is not None
        session.add(
            PolicyDecision(
                workspace_id=UUID(workspace_id),
                trace_id=inbox.trace_id,
                policy_version=3,
                effect=AclEffect.DENY,
                reason="test trace decision",
                request_snapshot={"resource_id": "builtin.echo"},
                matched_rule_ids=[],
            )
        )
        session.add(
            AuditEvent(
                workspace_id=UUID(workspace_id),
                trace_id=inbox.trace_id,
                actor_type="GROUP_MEMBER",
                actor_id="wxid_member",
                action="plugin.dispatch",
                object_type="plugin",
                object_id="builtin.echo",
                result="DENIED",
                detail={"reason": "test"},
            )
        )
        payload = {"text": "not sent", "at_wxids": []}
        session.add(
            OutboxMessage(
                bot_account_id=UUID(account_id),
                trace_id=inbox.trace_id,
                idempotency_key="trace-observability",
                action_type="message.reply.text",
                target_wxid="trace-room@chatroom",
                payload=payload,
                payload_sha256=hashlib.sha256(b"payload").hexdigest(),
                authorization_context={"sensitive_internal": "must-not-leak"},
            )
        )
        trace_id = inbox.trace_id

    response = await admin_client.get(f"/api/v1/traces/{trace_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["conversation_id"] == "trace-room@chatroom"
    assert body["policy_decisions"][0]["reason"] == "test trace decision"
    assert body["audit_events"][0]["result"] == "DENIED"
    assert body["outbox_messages"][0]["status"] == "PENDING"
    assert "authorization_context" not in response.text
    assert "must-not-leak" not in response.text


async def test_message_and_trace_not_found(admin_client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000001"
    assert (await admin_client.get(f"/api/v1/messages/{missing}")).status_code == 404
    assert (await admin_client.get(f"/api/v1/traces/{missing}")).status_code == 404
