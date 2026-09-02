from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from wechat_bot.plugins.manifest import PluginManifest, load_plugin_manifest

MAX_PROTOCOL_LINE_BYTES = 1_048_576
PLUGIN_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


def _plugin_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = source if source is not None else os.environ
    sanitized = {
        key: value for key, value in environment.items() if key.upper() in PLUGIN_ENV_ALLOWLIST
    }
    sanitized["PYTHONIOENCODING"] = "utf-8"
    sanitized["PYTHONUTF8"] = "1"
    return sanitized


class PluginRuntimeError(RuntimeError):
    pass


class PluginNotActiveError(PluginRuntimeError):
    pass


class StalePluginResultError(PluginRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PluginLaunchSpec:
    package_path: Path
    manifest: PluginManifest
    package_sha256: str
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_package(
        cls,
        package_path: Path,
        *,
        config: dict[str, Any] | None = None,
    ) -> PluginLaunchSpec:
        manifest, package_sha256 = load_plugin_manifest(package_path)
        return cls(
            package_path=package_path.resolve(strict=True),
            manifest=manifest,
            package_sha256=package_sha256,
            config=config or {},
        )


class PluginProcess:
    def __init__(self, spec: PluginLaunchSpec) -> None:
        self.spec = spec
        self._process: asyncio.subprocess.Process | None = None
        self._call_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)

    async def start(self) -> None:
        if self._process is not None:
            raise PluginRuntimeError("plugin process is already started")
        creation_flags = 0x08000000 if os.name == "nt" else 0
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "wechat_bot.plugins.runner",
            "--plugin-path",
            str(self.spec.package_path),
            "--entrypoint",
            self.spec.manifest.entrypoint,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.spec.package_path,
            creationflags=creation_flags,
            env=_plugin_subprocess_environment(),
            limit=MAX_PROTOCOL_LINE_BYTES + 1,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self.call(
                "initialize",
                {"config": self.spec.config},
                deadline_seconds=self.spec.manifest.timeout_seconds,
            )
            health = await self.call("health", deadline_seconds=self.spec.manifest.timeout_seconds)
            if not isinstance(health, dict) or health.get("status") not in {"ok", "ready"}:
                raise PluginRuntimeError("plugin health check did not return ready status")
        except BaseException:
            await self.stop(force=True)
            raise

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        deadline_seconds: float | None = None,
    ) -> Any:
        request_id = str(uuid4())
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PROTOCOL_LINE_BYTES:
            raise PluginRuntimeError("plugin request exceeds protocol limit")

        async with self._call_lock:
            process = self._process
            if process is None or process.returncode is not None:
                raise PluginRuntimeError("plugin process is not running")
            if process.stdin is None or process.stdout is None:
                await self._invalidate(process)
                raise PluginRuntimeError("plugin process pipes are unavailable")
            try:
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                message = self._diagnostic("plugin process transport failed")
                await self._invalidate(process)
                raise PluginRuntimeError(message) from exc
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=deadline_seconds or self.spec.manifest.timeout_seconds,
                )
            except TimeoutError as exc:
                message = self._diagnostic("plugin call timed out")
                await self._invalidate(process)
                raise PluginRuntimeError(message) from exc
            except (ValueError, asyncio.LimitOverrunError) as exc:
                message = self._diagnostic("plugin response exceeds protocol limit")
                await self._invalidate(process)
                raise PluginRuntimeError(message) from exc
            if not line or len(line) > MAX_PROTOCOL_LINE_BYTES:
                message = self._diagnostic("plugin returned no valid response")
                await self._invalidate(process)
                raise PluginRuntimeError(message)
            try:
                response = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                message = self._diagnostic("plugin returned invalid protocol JSON")
                await self._invalidate(process)
                raise PluginRuntimeError(message) from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                await self._invalidate(process)
                raise PluginRuntimeError("plugin response envelope did not match request")
            if response.get("ok") is not True:
                raise PluginRuntimeError(str(response.get("error") or "plugin call failed"))
            return response.get("result")

    async def stop(self, *, force: bool = False) -> None:
        if not force:
            process = self._process
            if process is None:
                return
            try:
                await self.call("shutdown", deadline_seconds=5)
            except PluginRuntimeError:
                force = True
        async with self._call_lock:
            process = self._process
            if process is None:
                return
            await self._close_process(process, terminate=force)

    async def _invalidate(self, process: asyncio.subprocess.Process) -> None:
        if self._process is not process:
            return
        await self._close_process(process, terminate=True)

    async def _close_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        terminate: bool,
    ) -> None:
        if self._process is process:
            self._process = None
        if process.returncode is None and terminate:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            await stderr_task

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip()[:500])

    def _diagnostic(self, message: str) -> str:
        if not self._stderr_tail:
            return message
        return f"{message}; runner reported: {self._stderr_tail[-1]}"


@dataclass(slots=True)
class _ActivePlugin:
    process: PluginProcess
    activation_epoch: int
    in_flight: int = 0
    accepting: bool = True
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.drained.set()


@dataclass(slots=True)
class PluginActivationPreparation:
    deployment_id: str
    activation_epoch: int
    spec: PluginLaunchSpec
    candidate: PluginProcess | None = None


@dataclass(slots=True)
class PluginDeactivationPreparation:
    deployment_id: str
    active: _ActivePlugin | None


class PluginSupervisor:
    def __init__(self) -> None:
        self._active: dict[str, _ActivePlugin] = {}
        self._last_epoch: dict[str, int] = {}
        self._pending_activations: dict[str, PluginActivationPreparation] = {}
        self._pending_deactivations: dict[str, PluginDeactivationPreparation] = {}
        self._lock = asyncio.Lock()

    async def prepare_activation(
        self,
        deployment_id: str,
        spec: PluginLaunchSpec,
        *,
        requested_epoch: int | None = None,
    ) -> PluginActivationPreparation:
        async with self._lock:
            self._reject_pending_transition(deployment_id)
            last_epoch = self._last_epoch.get(deployment_id, 0)
            epoch = requested_epoch if requested_epoch is not None else last_epoch + 1
            if epoch <= last_epoch:
                raise PluginRuntimeError("activation epoch must increase monotonically")
            preparation = PluginActivationPreparation(
                deployment_id=deployment_id,
                activation_epoch=epoch,
                spec=spec,
            )
            self._pending_activations[deployment_id] = preparation

        candidate = PluginProcess(spec)
        preparation.candidate = candidate
        try:
            await candidate.start()
        except BaseException:
            await self.abort_activation(preparation)
            raise
        async with self._lock:
            if self._pending_activations.get(deployment_id) is not preparation:
                invalidated = True
            else:
                invalidated = False
        if invalidated:
            await candidate.stop(force=True)
            raise PluginRuntimeError("plugin activation preparation is no longer valid")
        return preparation

    async def commit_activation(
        self,
        preparation: PluginActivationPreparation,
    ) -> int:
        deployment_id = preparation.deployment_id
        candidate = preparation.candidate
        if candidate is None:
            await self.abort_activation(preparation)
            raise PluginRuntimeError("plugin activation candidate is unavailable")
        async with self._lock:
            self._require_pending_activation(preparation)
            self._pending_activations.pop(deployment_id)
            self._last_epoch[deployment_id] = preparation.activation_epoch
            old = self._active.get(deployment_id)
            if old is not None:
                old.accepting = False
            self._active[deployment_id] = _ActivePlugin(
                candidate,
                preparation.activation_epoch,
            )
        if old is not None:
            await old.drained.wait()
            await old.process.stop()
        return preparation.activation_epoch

    async def abort_activation(
        self,
        preparation: PluginActivationPreparation,
    ) -> None:
        async with self._lock:
            if self._pending_activations.get(preparation.deployment_id) is not preparation:
                return
            self._pending_activations.pop(preparation.deployment_id)
            candidate = preparation.candidate
        if candidate is not None:
            await candidate.stop(force=True)

    async def activate(
        self,
        deployment_id: str,
        spec: PluginLaunchSpec,
        *,
        requested_epoch: int | None = None,
    ) -> int:
        preparation = await self.prepare_activation(
            deployment_id,
            spec,
            requested_epoch=requested_epoch,
        )
        try:
            return await self.commit_activation(preparation)
        except BaseException:
            await self.abort_activation(preparation)
            raise

    async def prepare_deactivation(
        self,
        deployment_id: str,
    ) -> PluginDeactivationPreparation:
        async with self._lock:
            self._reject_pending_transition(deployment_id)
            active = self._active.get(deployment_id)
            if active is not None:
                active.accepting = False
            preparation = PluginDeactivationPreparation(
                deployment_id=deployment_id,
                active=active,
            )
            self._pending_deactivations[deployment_id] = preparation
            return preparation

    async def commit_deactivation(
        self,
        preparation: PluginDeactivationPreparation,
    ) -> None:
        deployment_id = preparation.deployment_id
        async with self._lock:
            self._require_pending_deactivation(preparation)
            self._pending_deactivations.pop(deployment_id)
            active = preparation.active
            if active is not None and self._active.get(deployment_id) is active:
                self._active.pop(deployment_id)
        if active is not None:
            await active.drained.wait()
            await active.process.stop()

    async def abort_deactivation(
        self,
        preparation: PluginDeactivationPreparation,
    ) -> None:
        async with self._lock:
            if self._pending_deactivations.get(preparation.deployment_id) is not preparation:
                return
            self._pending_deactivations.pop(preparation.deployment_id)
            active = preparation.active
            if active is not None and self._active.get(preparation.deployment_id) is active:
                active.accepting = True

    async def deactivate(self, deployment_id: str) -> None:
        preparation = await self.prepare_deactivation(deployment_id)
        try:
            await self.commit_deactivation(preparation)
        except BaseException:
            await self.abort_deactivation(preparation)
            raise

    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        active = await self._acquire(deployment_id)
        try:
            result = await active.process.call(method, params)
        finally:
            await self._release(active)
        async with self._lock:
            current = self._active.get(deployment_id)
            if current is not active:
                raise StalePluginResultError("plugin result belongs to a stale activation")
        return active.activation_epoch, result

    async def shutdown(self) -> None:
        async with self._lock:
            pending_activations = list(self._pending_activations.values())
            pending_deactivations = list(self._pending_deactivations.values())
        for activation_preparation in pending_activations:
            await self.abort_activation(activation_preparation)
        for deactivation_preparation in pending_deactivations:
            await self.abort_deactivation(deactivation_preparation)
        async with self._lock:
            deployment_ids = list(self._active)
        for deployment_id in deployment_ids:
            await self.deactivate(deployment_id)

    def _reject_pending_transition(self, deployment_id: str) -> None:
        if (
            deployment_id in self._pending_activations
            or deployment_id in self._pending_deactivations
        ):
            raise PluginRuntimeError("plugin deployment already has a pending transition")

    def _require_pending_activation(
        self,
        preparation: PluginActivationPreparation,
    ) -> None:
        if self._pending_activations.get(preparation.deployment_id) is not preparation:
            raise PluginRuntimeError("plugin activation preparation is no longer valid")

    def _require_pending_deactivation(
        self,
        preparation: PluginDeactivationPreparation,
    ) -> None:
        if self._pending_deactivations.get(preparation.deployment_id) is not preparation:
            raise PluginRuntimeError("plugin deactivation preparation is no longer valid")

    async def _acquire(self, deployment_id: str) -> _ActivePlugin:
        async with self._lock:
            active = self._active.get(deployment_id)
            if active is None or not active.accepting:
                raise PluginNotActiveError("plugin deployment is not active")
            active.in_flight += 1
            active.drained.clear()
            return active

    async def _release(self, active: _ActivePlugin) -> None:
        async with self._lock:
            active.in_flight -= 1
            if active.in_flight == 0:
                active.drained.set()
