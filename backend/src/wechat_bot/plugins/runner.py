from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

MAX_PROTOCOL_LINE_BYTES = 1_048_576


class Runner:
    def __init__(self, *, package_path: Path, entrypoint: str, protocol_out: TextIO) -> None:
        self._package_path = package_path
        self._entrypoint = entrypoint
        self._protocol_out = protocol_out
        self._plugin: Any = None
        self._shutdown_requested = False

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(request_id, str) or not isinstance(method, str):
            return {"id": request_id, "ok": False, "error": "invalid request envelope"}
        if not isinstance(params, dict):
            return {"id": request_id, "ok": False, "error": "params must be an object"}
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = await self._dispatch(method, params)
            json.dumps(result, ensure_ascii=True)
            return {"id": request_id, "ok": True, "result": result}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return {"id": request_id, "ok": False, "error": "plugin call failed"}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            if self._plugin is not None:
                raise RuntimeError("plugin is already initialized")
            self._plugin = self._load_plugin()
            await self._optional_call("startup", params.get("config", {}))
            return {"status": "ready"}
        if self._plugin is None:
            raise RuntimeError("plugin is not initialized")
        if method == "health":
            result = await self._optional_call("health")
            return result if result is not None else {"status": "ok"}
        if method == "handle_event":
            return await self._required_call("handle_event", params.get("event", {}))
        if method == "invoke_tool":
            return await self._required_call(
                "invoke_tool",
                params.get("tool_name"),
                params.get("arguments", {}),
                params.get("context", {}),
            )
        if method == "shutdown":
            await self._optional_call("shutdown")
            self._shutdown_requested = True
            return {"status": "stopped"}
        raise RuntimeError("unsupported runner method")

    def _load_plugin(self) -> Any:
        sys.path.insert(0, str(self._package_path))
        module_name, _, factory_name = self._entrypoint.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        plugin = factory()
        if inspect.isawaitable(plugin):
            raise TypeError("plugin factory must be synchronous")
        return plugin

    async def _required_call(self, name: str, *args: Any) -> Any:
        handler = getattr(self._plugin, name, None)
        if handler is None or not callable(handler):
            raise RuntimeError(f"plugin does not implement {name}")
        result = handler(*args)
        return await result if inspect.isawaitable(result) else result

    async def _optional_call(self, name: str, *args: Any) -> Any:
        handler = getattr(self._plugin, name, None)
        if handler is None:
            return None
        if not callable(handler):
            raise RuntimeError(f"plugin attribute {name} is not callable")
        result = handler(*args)
        return await result if inspect.isawaitable(result) else result

    async def run(self) -> None:
        while not self._shutdown_requested:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_PROTOCOL_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_PROTOCOL_LINE_BYTES:
                response: dict[str, Any] = {
                    "id": None,
                    "ok": False,
                    "error": "request exceeds protocol limit",
                }
            else:
                try:
                    parsed = json.loads(line)
                    if not isinstance(parsed, dict):
                        raise ValueError
                    response = await self.handle(parsed)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    response = {"id": None, "ok": False, "error": "invalid JSON request"}
            self._protocol_out.write(json.dumps(response, ensure_ascii=True) + "\n")
            self._protocol_out.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-path", required=True, type=Path)
    parser.add_argument("--entrypoint", required=True)
    args = parser.parse_args()
    protocol_out = sys.stdout
    runner = Runner(
        package_path=args.plugin_path.resolve(strict=True),
        entrypoint=args.entrypoint,
        protocol_out=protocol_out,
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
