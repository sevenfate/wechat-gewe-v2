from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, cast

from websockets.asyncio.client import connect

from wechat_bot.maibot.constants import MAIBOT_PLATFORM
from wechat_bot.maibot.schemas import MaiBotConnectorConfig


class MaiBotWebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class MaiBotSocketFactory(Protocol):
    def __call__(self, config: MaiBotConnectorConfig) -> object: ...


@asynccontextmanager
async def open_maibot_socket(
    config: MaiBotConnectorConfig,
) -> AsyncIterator[MaiBotWebSocket]:
    # Keep authentication out of query parameters and out of maim-message's verbose logger.
    headers = {
        "x-apikey": config.api_key.get_secret_value(),
        "x-platform": MAIBOT_PLATFORM,
        "x-uuid": config.client_uuid,
    }
    async with connect(
        config.websocket_url,
        additional_headers=headers,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=1_048_576,
        max_queue=16,
    ) as socket:
        yield cast(MaiBotWebSocket, socket)
