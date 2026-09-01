from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


async def test_tool_bridge_management_endpoints_require_authentication(
    app: FastAPI,
    client: AsyncClient,
    admin_client: AsyncClient,
) -> None:
    del client
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous_client:
        unauthenticated = await anonymous_client.get("/api/v1/tool-bridge/catalog")
    assert unauthenticated.status_code == 401

    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Tool Bridge test connection",
            "api_base_url": "https://api.gewe.test",
            "token": "test-token-for-tool-bridge",
        },
    )
    assert connection.status_code == 201, connection.text

    catalog = await admin_client.get("/api/v1/tool-bridge/catalog")
    calls = await admin_client.get("/api/v1/tool-bridge/calls")
    assert catalog.status_code == 200, catalog.text
    assert calls.status_code == 200, calls.text
    assert catalog.json()["bridge_version"] == "1.0"
    assert catalog.json()["items"] == []
    assert calls.json() == {"items": [], "total": 0}
