from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from wechat_bot.db.models import OutboxMessage, OutboxStatus
from wechat_bot.events.dispatcher import command_resource_id


async def test_webhook_command_flows_through_acl_plugin_and_outbox(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Pipeline",
            "api_base_url": "https://api.gewe.test",
            "token": "pipeline-secret-token",
        },
    )
    connection_body = connection.json()
    account = await admin_client.post(
        f"/api/v1/connections/{connection_body['id']}/bot-accounts",
        json={"app_id": "app-pipeline", "wxid": "wxid_bot"},
    )
    account_id = account.json()["id"]

    installed = await admin_client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": connection_body["workspace_id"]},
    )
    created = await admin_client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": connection_body["workspace_id"],
            "plugin_id": installed.json()["plugin"]["id"],
            "package_version_id": installed.json()["package"]["id"],
            "name": "Pipeline Echo",
            "config": {"prefix": "bot:"},
            "scope": {"workspace_id": connection_body["workspace_id"]},
            "grants": ["message.reply.text"],
        },
    )
    deployment_id = created.json()["deployment"]["id"]
    revision_id = created.json()["revision"]["id"]
    activated = await admin_client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{revision_id}/activate"
    )
    assert activated.status_code == 200

    rule = await admin_client.post(
        "/api/v1/policy/rules",
        json={
            "workspace_id": connection_body["workspace_id"],
            "scope_type": "BOT_ACCOUNT",
            "scope_id": account_id,
            "resource_type": "COMMAND",
            "resource_id": command_resource_id("builtin.echo", "echo"),
            "effect": "ALLOW",
            "reason": "pipeline acceptance test",
        },
    )
    assert rule.status_code == 201

    callback_path = urlsplit(connection_body["callback_url"]).path
    callback = await admin_client.post(
        callback_path,
        json={
            "appid": "app-pipeline",
            "wxid": "wxid_bot",
            "content": "wxid_member:\n/echo hello",
            "createTime": 1_725_000_000,
            "fromUser": "pipeline-room@chatroom",
            "isSelf": False,
            "msgType": "TEXT",
            "newMsgId": 9_154_000_000_000_000_001,
            "toUser": "wxid_bot",
        },
    )
    assert callback.status_code == 200
    assert callback.text == ""

    attempted = await app.state.event_dispatcher_worker.run_once()
    second_attempt = await app.state.event_dispatcher_worker.run_once()

    database = app.state.database
    async with database.session_factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(OutboxMessage))
        message = await session.scalar(select(OutboxMessage))

    assert attempted == 1
    assert second_attempt == 0
    assert message_count == 1
    assert message is not None
    assert message.status is OutboxStatus.PENDING
    assert message.action_type == "message.reply.text"
    assert message.target_wxid == "pipeline-room@chatroom"
    assert message.payload == {"text": "bot:hello", "at_wxids": []}
    assert message.authorization_context is not None
    assert message.authorization_context["deployment_id"] == deployment_id
    assert message.authorization_context["deployment_revision_id"] == revision_id
    assert message.authorization_context["resource_type"] == "COMMAND"
    assert message.authorization_context["resource_id"] == command_resource_id(
        "builtin.echo", "echo"
    )
    assert message.authorization_context["actor_principal_id"] is not None
    assert message.authorization_context["chatroom_id"] is not None
    assert "pipeline-secret-token" not in str(message.payload)
