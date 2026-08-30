from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_checks_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readiness_reports_database_failure(
    client: AsyncClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state.database,
        "ping",
        AsyncMock(side_effect=RuntimeError("database details must not leak")),
    )

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
