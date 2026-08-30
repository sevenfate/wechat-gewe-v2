from __future__ import annotations

from typing import Any


class EchoPlugin:
    def __init__(self) -> None:
        self._prefix = ""

    async def startup(self, config: dict[str, Any]) -> None:
        prefix = config.get("prefix", "")
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a string")
        self._prefix = prefix

    async def health(self) -> dict[str, str]:
        return {"status": "ok"}

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        content = event.get("content", "")
        if not isinstance(content, str):
            raise ValueError("event content must be a string")
        return {
            "actions": [
                {
                    "type": "reply.text",
                    "content": f"{self._prefix}{content}",
                }
            ]
        }

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del context
        if tool_name != "plugin.echo.text":
            raise ValueError("unsupported tool")
        text = arguments.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        return {"text": f"{self._prefix}{text}"}

    async def shutdown(self) -> None:
        return None


def create_plugin() -> EchoPlugin:
    return EchoPlugin()
