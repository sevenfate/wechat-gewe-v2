from __future__ import annotations

from urllib.parse import urlsplit

import respx
from fastapi import FastAPI
from httpx import AsyncClient, Response


async def test_connection_create_list_duplicate_and_webhook(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    client = admin_client
    payload = {
        "name": "Primary",
        "api_base_url": "https://api.gewe.test",
        "token": "super-secret-token",
    }

    created = await client.post("/api/v1/connections", json=payload)
    duplicate = await client.post("/api/v1/connections", json=payload)
    listed = await client.get("/api/v1/connections")

    assert created.status_code == 201
    body = created.json()
    assert body["callback_mode"] == "MANUAL"
    assert body["callback_url"].startswith("http://testserver/webhooks/gewe/")
    assert "super-secret-token" not in created.text
    assert "token" not in body
    assert duplicate.status_code == 409
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    callback_path = urlsplit(body["callback_url"]).path
    callback = await client.post(callback_path, json={"test": "callback verification"})
    assert callback.status_code == 200
    assert await app.state.event_dispatcher_worker.run_once() == 1
    assert await app.state.event_dispatcher_worker.run_once() == 0


async def test_callback_requires_explicit_managed_mode(admin_client: AsyncClient) -> None:
    client = admin_client
    created = await client.post(
        "/api/v1/connections",
        json={
            "name": "Managed",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    connection_id = created.json()["id"]

    rejected = await client.post(f"/api/v1/connections/{connection_id}/callback/apply")
    assert rejected.status_code == 409

    switched = await client.put(
        f"/api/v1/connections/{connection_id}/callback-mode",
        json={"callback_mode": "PLATFORM_MANAGED"},
    )
    assert switched.status_code == 200

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.gewe.test/gewe/v2/api/login/setCallback").mock(
            return_value=Response(200, json={"ret": 200, "msg": "success", "data": {}})
        )
        applied = await client.post(f"/api/v1/connections/{connection_id}/callback/apply")

    assert route.called
    assert applied.status_code == 200
    assert applied.json()["applied"] is True
    assert (
        applied.json()["connection"]["callback_expected_url"]
        == applied.json()["connection"]["callback_url"]
    )


async def test_second_workspace_is_rejected_before_connection_creation(
    admin_client: AsyncClient,
) -> None:
    first = await admin_client.post(
        "/api/v1/connections",
        json={
            "workspace_slug": "default",
            "workspace_name": "默认工作区",
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "first-secret-token",
        },
    )
    second = await admin_client.post(
        "/api/v1/connections",
        json={
            "workspace_slug": "another",
            "workspace_name": "Another workspace",
            "name": "Secondary",
            "api_base_url": "https://api.gewe.test",
            "token": "second-secret-token",
        },
    )
    listed = await admin_client.get("/api/v1/connections")

    assert first.status_code == 201
    assert second.status_code == 409
    assert "one workspace" in second.json()["detail"]
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
