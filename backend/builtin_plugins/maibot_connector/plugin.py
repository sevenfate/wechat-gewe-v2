from __future__ import annotations

from typing import Any


class MaiBotConnectorMarker:
    """Marker entrypoint; the core routes this package to the managed WS runtime."""

    async def startup(self, config: dict[str, Any]) -> None:
        del config

    async def health(self) -> dict[str, str]:
        return {"status": "ready"}

    async def handle_event(self, event: dict[str, Any]) -> dict[str, list[object]]:
        del event
        return {"actions": []}

    async def shutdown(self) -> None:
        return None


def create_plugin() -> MaiBotConnectorMarker:
    return MaiBotConnectorMarker()
