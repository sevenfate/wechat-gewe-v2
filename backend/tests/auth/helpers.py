from __future__ import annotations

from httpx import AsyncClient, Response

BOOTSTRAP_TOKEN = "bootstrap-token-with-at-least-32-characters"
OWNER_USERNAME = "platform-owner"
OWNER_PASSWORD = "correct horse battery staple"


async def login_client(
    client: AsyncClient,
    *,
    username: str = OWNER_USERNAME,
    password: str = OWNER_PASSWORD,
) -> tuple[str, Response]:
    csrf_response = await client.get("/api/auth/csrf")
    assert csrf_response.status_code == 200
    csrf_token = csrf_response.json()["csrf_token"]
    response = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": username, "password": password},
    )
    return csrf_token, response
