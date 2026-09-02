from __future__ import annotations

from pathlib import Path

import pytest

from wechat_bot.plugins.manifest import PluginManifest, load_plugin_manifest
from wechat_bot.plugins.supervisor import (
    PluginLaunchSpec,
    PluginNotActiveError,
    PluginProcess,
    PluginRuntimeError,
    PluginSupervisor,
    _plugin_subprocess_environment,
)

ECHO_PACKAGE = Path(__file__).parents[2] / "builtin_plugins" / "echo"


def test_manifest_is_valid_and_hash_is_stable() -> None:
    manifest, first_hash = load_plugin_manifest(ECHO_PACKAGE)
    _, second_hash = load_plugin_manifest(ECHO_PACKAGE)

    assert manifest.plugin_id == "builtin.echo"
    assert manifest.tools[0].effect_class == "READ_ONLY"
    assert first_hash == second_hash
    assert len(first_hash) == 64


def test_plugin_subprocess_environment_drops_core_secrets() -> None:
    environment = _plugin_subprocess_environment(
        {
            "PATH": "test-path",
            "SYSTEMROOT": "test-system-root",
            "WECHAT_BOT_MASTER_KEY": "master-secret",
            "WECHAT_BOT_AUTH_BOOTSTRAP_TOKEN": "bootstrap-secret",
            "DATABASE_URL": "database-secret",
            "GEWE_TOKEN": "gewe-secret",
            "PYTHONPATH": "unsafe-import-path",
        }
    )

    assert environment == {
        "PATH": "test-path",
        "SYSTEMROOT": "test-system-root",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }


def test_manifest_rejects_command_name_and_alias_unicode_casefold_collision() -> None:
    with pytest.raises(ValueError, match="unique after Unicode casefold"):
        PluginManifest.model_validate(
            {
                "id": "test.casefold-collision",
                "name": "Casefold collision",
                "version": "1.0.0",
                "core_api": "1",
                "entrypoint": "plugin:create_plugin",
                "commands": [
                    {"name": "Straße", "aliases": []},
                    {"name": "other", "aliases": ["STRASSE"]},
                ],
            }
        )


@pytest.mark.parametrize(
    "command",
    [
        {"name": "   ", "aliases": []},
        {"name": "valid", "aliases": ["  "]},
    ],
)
def test_manifest_rejects_blank_command_names_and_aliases(
    command: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        PluginManifest.model_validate(
            {
                "id": "test.blank-command",
                "name": "Blank command",
                "version": "1.0.0",
                "core_api": "1",
                "entrypoint": "plugin:create_plugin",
                "commands": [command],
            }
        )


async def test_plugin_can_hot_activate_replace_and_deactivate() -> None:
    supervisor = PluginSupervisor()
    try:
        first_epoch = await supervisor.activate(
            "echo-deployment",
            PluginLaunchSpec.from_package(ECHO_PACKAGE, config={"prefix": "v1:"}),
        )
        result_epoch, first = await supervisor.call(
            "echo-deployment",
            "handle_event",
            {"event": {"content": "hello"}},
        )
        second_epoch = await supervisor.activate(
            "echo-deployment",
            PluginLaunchSpec.from_package(ECHO_PACKAGE, config={"prefix": "v2:"}),
        )
        _, second = await supervisor.call(
            "echo-deployment",
            "invoke_tool",
            {
                "tool_name": "plugin.echo.text",
                "arguments": {"text": "hello"},
                "context": {},
            },
        )
        await supervisor.deactivate("echo-deployment")

        assert first_epoch == result_epoch == 1
        assert first["actions"][0]["content"] == "v1:hello"
        assert second_epoch == 2
        assert second == {"text": "v2:hello"}
        with pytest.raises(PluginNotActiveError):
            await supervisor.call("echo-deployment", "health")
    finally:
        await supervisor.shutdown()


async def test_failed_candidate_keeps_active_plugin() -> None:
    supervisor = PluginSupervisor()
    try:
        spec = PluginLaunchSpec.from_package(ECHO_PACKAGE, config={"prefix": "stable:"})
        await supervisor.activate("echo-deployment", spec)
        broken = PluginLaunchSpec(
            package_path=spec.package_path,
            manifest=spec.manifest.model_copy(update={"entrypoint": "missing:create"}),
            package_sha256=spec.package_sha256,
        )

        with pytest.raises(PluginRuntimeError):
            await supervisor.activate("echo-deployment", broken)

        epoch, result = await supervisor.call(
            "echo-deployment",
            "handle_event",
            {"event": {"content": "still works"}},
        )
        assert epoch == 1
        assert result["actions"][0]["content"] == "stable:still works"
    finally:
        await supervisor.shutdown()


async def test_aborted_transitions_keep_current_plugin_available() -> None:
    supervisor = PluginSupervisor()
    try:
        stable = PluginLaunchSpec.from_package(ECHO_PACKAGE, config={"prefix": "stable:"})
        await supervisor.activate("echo-deployment", stable)

        candidate = await supervisor.prepare_activation(
            "echo-deployment",
            PluginLaunchSpec.from_package(ECHO_PACKAGE, config={"prefix": "candidate:"}),
            requested_epoch=2,
        )
        await supervisor.abort_activation(candidate)
        deactivation = await supervisor.prepare_deactivation("echo-deployment")
        await supervisor.abort_deactivation(deactivation)

        epoch, result = await supervisor.call(
            "echo-deployment",
            "handle_event",
            {"event": {"content": "still works"}},
        )
        assert epoch == 1
        assert result["actions"][0]["content"] == "stable:still works"
    finally:
        await supervisor.shutdown()


async def test_timed_out_process_is_invalidated_before_next_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_path = _write_slow_plugin(tmp_path)
    monkeypatch.setenv("WECHAT_BOT_MASTER_KEY", "must-not-reach-plugin")
    monkeypatch.setenv("WECHAT_BOT_AUTH_BOOTSTRAP_TOKEN", "must-not-reach-plugin")
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-plugin")
    process = PluginProcess(PluginLaunchSpec.from_package(package_path))
    await process.start()
    try:
        environment = await process.call(
            "handle_event",
            {"event": {"delay": 0}},
            deadline_seconds=0.5,
        )
        assert environment == {
            "database_url": None,
            "has_path": True,
            "master_key": None,
            "bootstrap_token": None,
        }
        with pytest.raises(PluginRuntimeError, match="timed out"):
            await process.call(
                "handle_event",
                {"event": {"delay": 0.2}},
                deadline_seconds=0.02,
            )
        with pytest.raises(PluginRuntimeError, match="not running"):
            await process.call(
                "handle_event",
                {"event": {"delay": 0}},
                deadline_seconds=0.5,
            )
    finally:
        await process.stop(force=True)


def _write_slow_plugin(tmp_path: Path) -> Path:
    package_path = tmp_path / "slow_plugin"
    package_path.mkdir()
    (package_path / "manifest.toml").write_text(
        "\n".join(
            (
                'id = "test.slow"',
                'name = "Slow test plugin"',
                'version = "1.0.0"',
                'core_api = "1"',
                'entrypoint = "plugin:create_plugin"',
                "timeout_seconds = 1",
            )
        ),
        encoding="utf-8",
    )
    (package_path / "plugin.py").write_text(
        """from __future__ import annotations

import asyncio
import os


class SlowPlugin:
    async def health(self):
        return {"status": "ok"}

    async def handle_event(self, event):
        await asyncio.sleep(event.get("delay", 0))
        return {
            "database_url": os.environ.get("DATABASE_URL"),
            "has_path": bool(os.environ.get("PATH")),
            "master_key": os.environ.get("WECHAT_BOT_MASTER_KEY"),
            "bootstrap_token": os.environ.get("WECHAT_BOT_AUTH_BOOTSTRAP_TOKEN"),
        }


def create_plugin():
    return SlowPlugin()
""",
        encoding="utf-8",
    )
    return package_path
