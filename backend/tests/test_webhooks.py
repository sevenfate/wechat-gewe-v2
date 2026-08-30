from __future__ import annotations

import hashlib

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from wechat_bot.core.config import Settings
from wechat_bot.db.base import Base
from wechat_bot.db.models import (
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    WebhookInbox,
    Workspace,
)
from wechat_bot.main import create_app


async def test_webhook_persists_and_deduplicates_v2_payload(settings: Settings) -> None:
    callback_secret = "test-callback-secret"
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        database = app.state.database
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with database.session_factory() as session:
            workspace = Workspace(name="Default", slug="default")
            session.add(workspace)
            await session.flush()
            session.add(
                GeweConnection(
                    workspace_id=workspace.id,
                    name="Primary",
                    api_base_url="https://api.example.test",
                    token_ciphertext=b"encrypted",
                    token_fingerprint="0123456789abcdef",
                    callback_secret_ciphertext=b"encrypted",
                    callback_secret_hash=hashlib.sha256(
                        callback_secret.encode("utf-8")
                    ).hexdigest(),
                )
            )
            await session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            payload = {
                "appid": "app-1",
                "wxid": "wxid_bot",
                "content": "hello",
                "createTime": 1_725_000_000,
                "fromUser": "wxid_friend",
                "isSelf": False,
                "msgType": "TEXT",
                "newMsgId": 9_154_000_000_000_000_001,
                "toUser": "wxid_bot",
            }
            first = await client.post(f"/webhooks/gewe/{callback_secret}", json=payload)
            duplicate = await client.post(f"/webhooks/gewe/{callback_secret}", json=payload)
            conflict = await client.post(
                f"/webhooks/gewe/{callback_secret}",
                json={**payload, "content": "same id, different message"},
            )

        assert first.status_code == 200
        assert first.text == ""
        assert duplicate.status_code == 200
        assert conflict.status_code == 409
        assert "different payload" in conflict.json()["detail"]

        async with database.session_factory() as session:
            inbox_count = await session.scalar(select(func.count()).select_from(WebhookInbox))
            event_count = await session.scalar(select(func.count()).select_from(NormalizedEvent))
            inbox = await session.scalar(select(WebhookInbox))
            event = await session.scalar(select(NormalizedEvent))

        assert inbox_count == 1
        assert event_count == 1
        assert inbox is not None
        assert inbox.new_msg_id == "9154000000000000001"
        assert inbox.status is InboxStatus.NORMALIZED
        assert event is not None
        assert event.provider_message_id == "9154000000000000001"


async def test_webhook_rejects_unknown_secret_and_oversized_body(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        database = app.state.database
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            unknown = await client.post("/webhooks/gewe/not-configured", json={"test": True})
            oversized = await client.post(
                "/webhooks/gewe/not-configured",
                content=b"x" * (settings.webhook_max_body_bytes + 1),
                headers={"content-type": "application/json"},
            )

    assert unknown.status_code == 404
    assert oversized.status_code == 413
