from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.constants import CSRF_HEADER_NAME
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.plugin_models import (
    PluginActivationStatus,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginPackageVersion,
    PluginRevisionActivation,
)
from wechat_bot.plugins.catalog import (
    PLUGIN_REDACTION_MARKER,
    SCOPE_FILTER_KEYS,
    PluginCatalogService,
    _redact_sensitive_config,
    _restore_secret_placeholders,
    _scopes_overlap,
)
from wechat_bot.plugins.supervisor import PluginRuntimeError, PluginSupervisor


async def test_builtin_plugin_deployment_hot_upgrade_and_stop(
    admin_client: AsyncClient,
) -> None:
    client = admin_client
    connection = await client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    workspace_id = connection.json()["workspace_id"]
    installed = await client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": workspace_id},
    )
    assert installed.status_code == 201
    assert "package_path" not in installed.text
    plugin_id = installed.json()["plugin"]["id"]
    package_id = installed.json()["package"]["id"]

    created = await client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": plugin_id,
            "package_version_id": package_id,
            "name": "Echo",
            "config": {"prefix": "v1:"},
            "scope": {"workspace_id": workspace_id},
            "grants": ["message.reply.text"],
        },
    )
    assert created.status_code == 201
    deployment_id = created.json()["deployment"]["id"]
    first_revision_id = created.json()["revision"]["id"]

    first_activation = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{first_revision_id}/activate"
    )
    first_call = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/invoke",
        json={
            "method": "handle_event",
            "params": {"event": {"content": "hello"}},
        },
    )
    assert first_activation.status_code == 200
    assert first_activation.json()["activation"]["activation_epoch"] == 1
    assert first_call.json()["result"]["actions"][0]["content"] == "v1:hello"

    second_revision = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions",
        json={
            "package_version_id": package_id,
            "config": {"prefix": "v2:"},
            "scope": {"workspace_id": workspace_id},
            "grants": ["message.reply.text"],
        },
    )
    second_revision_id = second_revision.json()["id"]
    second_activation = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{second_revision_id}/activate"
    )
    second_call = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/invoke",
        json={
            "method": "invoke_tool",
            "params": {
                "tool_name": "plugin.echo.text",
                "arguments": {"text": "hello"},
                "context": {},
            },
        },
    )
    stopped = await client.post(f"/api/v1/plugins/deployments/{deployment_id}/deactivate")
    after_stop = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/invoke",
        json={"method": "health"},
    )

    assert second_activation.status_code == 200
    assert second_activation.json()["activation"]["activation_epoch"] == 2
    assert second_call.json()["result"] == {"text": "v2:hello"}
    assert stopped.json()["status"] == "STOPPED"
    assert after_stop.status_code == 409

    catalog = await client.get("/api/v1/plugins")
    assert catalog.status_code == 200
    revisions = catalog.json()["revisions"]
    assert [item["revision_number"] for item in revisions] == [2, 1]
    assert {item["id"] for item in revisions} == {
        first_revision_id,
        second_revision_id,
    }
    assert "config_ciphertext" not in catalog.text
    assert "package_path" not in catalog.text


async def test_revision_draft_clones_source_and_preserves_secret(
    app: FastAPI,
    admin_client: AsyncClient,
    settings: Settings,
) -> None:
    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    workspace_id = connection.json()["workspace_id"]
    installed = await admin_client.post(
        "/api/v1/plugins/builtins/builtin.maibot-connector/install",
        json={"workspace_id": workspace_id},
    )
    package_id = installed.json()["package"]["id"]
    api_key = "maibot-secret-that-must-not-leak"
    config = {
        "websocket_url": "wss://maibot.test/ws",
        "api_key": api_key,
        "client_uuid": "stable-client",
        "message_ttl_seconds": 300,
        "max_pending_messages": 500,
        "ack_retry_seconds": 5,
        "reconnect_initial_seconds": 1,
        "reconnect_max_seconds": 20,
        "enable_proactive_messages": True,
    }
    scope = {"workspace_id": workspace_id, "chatroom_ids": ["group-a"]}
    grants = ["message.forward.external.maibot", "message.reply.text"]
    created = await admin_client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": installed.json()["plugin"]["id"],
            "package_version_id": package_id,
            "name": "MaiBot safe revisions",
            "config": config,
            "scope": scope,
            "grants": grants,
        },
    )
    assert created.status_code == 201, created.text
    deployment_id = created.json()["deployment"]["id"]
    source_revision_id = created.json()["revision"]["id"]

    draft = await admin_client.get(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{source_revision_id}/draft"
    )
    assert draft.status_code == 200
    assert draft.json() == {
        "source_revision_id": source_revision_id,
        "package_version_id": package_id,
        "config": {**config, "api_key": PLUGIN_REDACTION_MARKER},
        "scope": scope,
        "grants": grants,
        "secret_placeholder": PLUGIN_REDACTION_MARKER,
    }
    assert api_key not in draft.text

    cloned = await admin_client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions",
        json={"source_revision_id": source_revision_id},
    )
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["package_version_id"] == package_id
    assert cloned.json()["scope"] == scope
    assert cloned.json()["grants"] == grants

    edited = await admin_client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions",
        json={
            "source_revision_id": source_revision_id,
            "package_version_id": package_id,
            "config": {
                "websocket_url": "wss://maibot.test/v2",
                "api_key": PLUGIN_REDACTION_MARKER,
            },
            "scope": scope,
            "grants": grants,
        },
    )
    assert edited.status_code == 201, edited.text

    cipher = CredentialCipher.from_settings(settings)
    async with app.state.database.session_factory() as session:
        cloned_revision = await session.get(
            PluginDeploymentRevision,
            UUID(cloned.json()["id"]),
        )
        edited_revision = await session.get(
            PluginDeploymentRevision,
            UUID(edited.json()["id"]),
        )
    assert cloned_revision is not None
    assert edited_revision is not None
    assert json.loads(cipher.decrypt(cloned_revision.config_ciphertext)) == config
    assert json.loads(cipher.decrypt(edited_revision.config_ciphertext)) == {
        **config,
        "websocket_url": "wss://maibot.test/v2",
    }
    assert PLUGIN_REDACTION_MARKER not in cipher.decrypt(edited_revision.config_ciphertext)

    catalog = await admin_client.get("/api/v1/plugins")
    assert catalog.status_code == 200
    assert api_key not in catalog.text

    async with app.state.database.session_factory() as session:
        package = await session.get(PluginPackageVersion, UUID(package_id))
        assert package is not None
        manifest = json.loads(json.dumps(package.manifest))
        manifest["config_schema"]["properties"]["api_key"]["enum"] = ["allowed-value"]
        package.manifest = manifest
        await session.commit()
    invalid = await admin_client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions",
        json={"source_revision_id": source_revision_id},
    )
    assert invalid.status_code == 409
    assert api_key not in invalid.text


def test_nested_sensitive_config_is_redacted_and_restored() -> None:
    config = {
        "credentials": {
            "password": "nested-password",
            "label": "visible",
        },
        "providers": [{"api_key": "nested-api-key", "name": "primary"}],
        "schema_marked": "schema-secret",
        "write_only_marked": "write-only-secret",
        "password_formatted": "password-format-secret",
    }
    schema = {
        "type": "object",
        "properties": {
            "credentials": {
                "type": "object",
                "properties": {
                    "password": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
            "providers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string"},
                        "name": {"type": "string"},
                    },
                },
            },
            "schema_marked": {"type": "string", "x-sensitive": True},
            "write_only_marked": {"type": "string", "writeOnly": True},
            "password_formatted": {"type": "string", "format": "password"},
        },
    }

    redacted = _redact_sensitive_config(config, schema)

    assert redacted == {
        "credentials": {"password": PLUGIN_REDACTION_MARKER, "label": "visible"},
        "providers": [{"api_key": PLUGIN_REDACTION_MARKER, "name": "primary"}],
        "schema_marked": PLUGIN_REDACTION_MARKER,
        "write_only_marked": PLUGIN_REDACTION_MARKER,
        "password_formatted": PLUGIN_REDACTION_MARKER,
    }
    redacted["credentials"]["label"] = "updated"
    assert _restore_secret_placeholders(redacted, config) == {
        **config,
        "credentials": {"password": "nested-password", "label": "updated"},
    }


async def test_revision_source_must_belong_to_the_same_deployment(
    admin_client: AsyncClient,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    first = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="First source",
        scope={"workspace_id": workspace_id},
        prefix="first:",
    )
    second = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Second target",
        scope={"workspace_id": workspace_id},
        prefix="second:",
    )

    response = await admin_client.post(
        f"/api/v1/plugins/deployments/{second['deployment_id']}/revisions",
        json={"source_revision_id": first["revision_id"]},
    )
    draft = await admin_client.get(
        f"/api/v1/plugins/deployments/{second['deployment_id']}/revisions/"
        f"{first['revision_id']}/draft"
    )

    assert response.status_code == 404
    assert draft.status_code == 404


async def test_plugin_reader_context_does_not_require_connection_read(
    admin_client: AsyncClient,
) -> None:
    workspace_id, _, _ = await _install_echo(admin_client)
    role = await admin_client.post(
        "/api/v1/admin/roles",
        json={"code": "plugin-reader", "name": "Plugin Reader"},
    )
    assert role.status_code == 201
    binding = await admin_client.put(
        f"/api/v1/admin/roles/{role.json()['id']}/permissions",
        json={"permission_codes": ["plugin.read"]},
    )
    assert binding.status_code == 200
    user = await admin_client.post(
        "/api/v1/admin/users",
        json={
            "username": "plugin-reader",
            "display_name": "Plugin Reader",
            "password": "plugin reader password 123",
        },
    )
    assert user.status_code == 201
    user_binding = await admin_client.put(
        f"/api/v1/admin/users/{user.json()['id']}/roles",
        json={"role_codes": ["plugin-reader"]},
    )
    assert user_binding.status_code == 200

    assert (await admin_client.post("/api/auth/logout")).status_code == 200
    csrf = await admin_client.get("/api/auth/csrf")
    login = await admin_client.post(
        "/api/auth/login",
        headers={CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
        json={"username": "plugin-reader", "password": "plugin reader password 123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["permissions"] == ["plugin.read"]
    admin_client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]

    context = await admin_client.get("/api/v1/plugins/context")
    catalog = await admin_client.get("/api/v1/plugins")
    connections = await admin_client.get("/api/v1/connections")

    assert context.status_code == 200
    assert context.json()["workspace_id"] == workspace_id
    assert context.json()["name"]
    assert catalog.status_code == 200
    assert connections.status_code == 403


async def test_running_deployment_can_hot_rollback_to_an_old_revision(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    stable = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Rollback target",
        scope={"workspace_id": workspace_id},
        prefix="v1:",
    )
    first_activation = await _activate(admin_client, stable)
    assert first_activation.json()["activation"]["activation_epoch"] == 1

    candidate = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions",
        json={
            "source_revision_id": stable["revision_id"],
            "config": {"prefix": "v2:"},
        },
    )
    assert candidate.status_code == 201
    upgraded = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions/"
        f"{candidate.json()['id']}/activate"
    )
    assert upgraded.json()["activation"]["activation_epoch"] == 2
    await _assert_echo_result(admin_client, stable["deployment_id"], "v2:before rollback")

    rolled_back = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions/"
        f"{stable['revision_id']}/activate"
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["activation"]["activation_epoch"] == 3
    await _assert_echo_result(admin_client, stable["deployment_id"], "v1:after rollback")

    async with app.state.database.session_factory() as session:
        deployment = await session.get(PluginDeployment, UUID(stable["deployment_id"]))
        active_rows = list(
            await session.scalars(
                select(PluginRevisionActivation).where(
                    PluginRevisionActivation.deployment_id == UUID(stable["deployment_id"]),
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                )
            )
        )
    assert deployment is not None
    assert deployment.status == PluginDeploymentStatus.RUNNING
    assert str(deployment.active_revision_id) == stable["revision_id"]
    assert len(active_rows) == 1
    assert str(active_rows[0].revision_id) == stable["revision_id"]


async def test_failed_candidate_keeps_old_revision_and_runner_active(
    app: FastAPI,
    admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    stable = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Candidate failure",
        scope={"workspace_id": workspace_id},
        prefix="stable:",
    )
    assert (await _activate(admin_client, stable)).status_code == 200
    candidate = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions",
        json={
            "source_revision_id": stable["revision_id"],
            "config": {"prefix": "candidate:"},
        },
    )
    assert candidate.status_code == 201

    supervisor: PluginSupervisor = app.state.plugin_supervisor
    original_prepare = supervisor.prepare_activation

    async def fail_candidate(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PluginRuntimeError("injected candidate startup failure")

    monkeypatch.setattr(supervisor, "prepare_activation", fail_candidate)
    failed = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions/"
        f"{candidate.json()['id']}/activate"
    )
    monkeypatch.setattr(supervisor, "prepare_activation", original_prepare)

    assert failed.status_code == 409
    assert failed.json()["detail"] == "candidate plugin activation failed"
    await _assert_echo_result(admin_client, stable["deployment_id"], "stable:still active")

    async with app.state.database.session_factory() as session:
        deployment = await session.get(PluginDeployment, UUID(stable["deployment_id"]))
        active_rows = list(
            await session.scalars(
                select(PluginRevisionActivation).where(
                    PluginRevisionActivation.deployment_id == UUID(stable["deployment_id"]),
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                )
            )
        )
        failed_rows = list(
            await session.scalars(
                select(PluginRevisionActivation).where(
                    PluginRevisionActivation.revision_id == UUID(candidate.json()["id"]),
                    PluginRevisionActivation.status == PluginActivationStatus.FAILED,
                )
            )
        )
    assert deployment is not None
    assert deployment.status == PluginDeploymentStatus.RUNNING
    assert str(deployment.active_revision_id) == stable["revision_id"]
    assert len(active_rows) == 1
    assert str(active_rows[0].revision_id) == stable["revision_id"]
    assert len(failed_rows) == 1


async def test_deployment_rejects_undeclared_capability(admin_client: AsyncClient) -> None:
    client = admin_client
    connection = await client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    workspace_id = connection.json()["workspace_id"]
    installed = await client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": workspace_id},
    )

    response = await client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": installed.json()["plugin"]["id"],
            "package_version_id": installed.json()["package"]["id"],
            "name": "Overprivileged",
            "grants": ["system.acl.write"],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "deployment grants exceed manifest capabilities"


async def test_deployment_scope_is_strictly_validated_and_canonicalized(
    admin_client: AsyncClient,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    invalid_scopes: list[tuple[dict[str, object], str]] = [
        ({"unexpected": ["value"]}, "unknown key"),
        ({"workspace_id": str(uuid4())}, "does not match deployment"),
        ({"workspace_id": "not-a-uuid"}, "must be a UUID string"),
        ({"workspace_id": None}, "must be a UUID string"),
        ({"chatroom_ids": "group-a"}, "must be a list of strings"),
        ({"contact_ids": ["contact-a", " "]}, "non-empty strings"),
        ({"conversation_ids": ["conversation-a", 42]}, "non-empty strings"),
    ]
    for index, (scope, expected_error) in enumerate(invalid_scopes):
        response = await admin_client.post(
            "/api/v1/plugins/deployments",
            json={
                "workspace_id": workspace_id,
                "plugin_id": plugin_id,
                "package_version_id": package_id,
                "name": f"Invalid scope {index}",
                "scope": scope,
                "grants": ["message.reply.text"],
            },
        )
        assert response.status_code == 409
        assert expected_error in response.json()["detail"]

    valid = await admin_client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": plugin_id,
            "package_version_id": package_id,
            "name": "Canonical scope",
            "scope": {
                "workspace_id": workspace_id.upper(),
                "bot_account_ids": [" account-b ", "account-a", "account-a"],
            },
            "grants": ["message.reply.text"],
        },
    )
    assert valid.status_code == 201
    assert valid.json()["revision"]["scope"] == {
        "workspace_id": workspace_id,
        "bot_account_ids": ["account-a", "account-b"],
    }


async def test_runtime_state_survives_activation_and_deactivation_commit_failures(
    app: FastAPI,
    admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    stable = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Commit failure",
        scope={"workspace_id": workspace_id},
        prefix="stable:",
    )
    assert (await _activate(admin_client, stable)).status_code == 200
    candidate = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions",
        json={
            "package_version_id": package_id,
            "config": {"prefix": "candidate:"},
            "scope": {"workspace_id": workspace_id},
            "grants": ["message.reply.text"],
        },
    )
    assert candidate.status_code == 201

    original_commit = AsyncSession.commit

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        await admin_client.post(
            f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions/"
            f"{candidate.json()['id']}/activate"
        )
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    await _assert_echo_result(admin_client, stable["deployment_id"], "stable:after activate")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        await admin_client.post(f"/api/v1/plugins/deployments/{stable['deployment_id']}/deactivate")
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    await _assert_echo_result(admin_client, stable["deployment_id"], "stable:after stop")

    async with app.state.database.session_factory() as session:
        deployment = await session.get(
            PluginDeployment,
            UUID(stable["deployment_id"]),
        )
        candidate_activation_count = await session.scalar(
            select(func.count(PluginRevisionActivation.id)).where(
                PluginRevisionActivation.revision_id == UUID(candidate.json()["id"])
            )
        )
    assert deployment is not None
    assert deployment.status == PluginDeploymentStatus.RUNNING
    assert str(deployment.active_revision_id) == stable["revision_id"]
    assert candidate_activation_count == 0


async def test_running_deployment_is_reactivated_after_core_restart(
    app: FastAPI,
    admin_client: AsyncClient,
    settings: Settings,
) -> None:
    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    workspace_id = connection.json()["workspace_id"]
    installed = await admin_client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": workspace_id},
    )
    created = await admin_client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": installed.json()["plugin"]["id"],
            "package_version_id": installed.json()["package"]["id"],
            "name": "Echo restore",
            "config": {"prefix": "restored:"},
            "scope": {"workspace_id": workspace_id},
            "grants": ["message.reply.text"],
        },
    )
    deployment_id = created.json()["deployment"]["id"]
    revision_id = created.json()["revision"]["id"]
    activated = await admin_client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/revisions/{revision_id}/activate"
    )
    assert activated.json()["activation"]["activation_epoch"] == 1

    await app.state.plugin_supervisor.shutdown()
    restarted_supervisor = PluginSupervisor()
    try:
        database = app.state.database
        async with database.session_factory() as session:
            result = await PluginCatalogService(
                CredentialCipher.from_settings(settings)
            ).restore_active_deployments(
                session,
                supervisor=restarted_supervisor,
            )
            await session.commit()
        for preparation in result.runtime_activations:
            await restarted_supervisor.commit_activation(preparation)

        epoch, response = await restarted_supervisor.call(
            deployment_id,
            "handle_event",
            {"event": {"content": "hello"}},
        )
        assert len(result.restored_deployment_ids) == 1
        assert result.failed_deployment_ids == ()
        assert epoch == 2
        assert response["actions"][0]["content"] == "restored:hello"
    finally:
        await restarted_supervisor.shutdown()


async def test_first_activation_rejects_command_conflict_in_overlapping_scope(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    first = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Account A",
        scope={"bot_account_ids": ["account-a"], "chatroom_ids": ["group-a"]},
    )
    overlapping = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Account A global groups",
        scope={"bot_account_ids": ["account-a"]},
    )
    disjoint = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Account B",
        scope={"bot_account_ids": ["account-b"]},
    )

    assert (await _activate(admin_client, first)).status_code == 200
    rejected = await _activate(admin_client, overlapping)
    assert rejected.status_code == 409
    assert "command 'echo' conflicts" in rejected.json()["detail"]

    async with app.state.database.session_factory() as session:
        rejected_deployment = await session.get(
            PluginDeployment,
            UUID(overlapping["deployment_id"]),
        )
        activation_count = await session.scalar(
            select(func.count(PluginRevisionActivation.id)).where(
                PluginRevisionActivation.deployment_id == UUID(overlapping["deployment_id"])
            )
        )
    assert rejected_deployment is not None
    assert rejected_deployment.status == PluginDeploymentStatus.STOPPED
    assert rejected_deployment.active_revision_id is None
    assert activation_count == 0

    assert (await _activate(admin_client, disjoint)).status_code == 200


async def test_hot_upgrade_command_conflict_keeps_current_revision_running(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    workspace_id, plugin_id, package_id = await _install_echo(admin_client)
    other = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Other account",
        scope={"bot_account_ids": ["account-a"]},
        prefix="other:",
    )
    stable = await _create_echo_deployment(
        admin_client,
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        package_id=package_id,
        name="Stable account",
        scope={"bot_account_ids": ["account-b"]},
        prefix="stable:",
    )
    assert (await _activate(admin_client, other)).status_code == 200
    assert (await _activate(admin_client, stable)).status_code == 200

    candidate = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions",
        json={
            "package_version_id": package_id,
            "config": {"prefix": "candidate:"},
            "scope": {},
            "grants": ["message.reply.text"],
        },
    )
    assert candidate.status_code == 201
    rejected = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/revisions/"
        f"{candidate.json()['id']}/activate"
    )
    assert rejected.status_code == 409
    assert "command 'echo' conflicts" in rejected.json()["detail"]

    invocation = await admin_client.post(
        f"/api/v1/plugins/deployments/{stable['deployment_id']}/invoke",
        json={
            "method": "handle_event",
            "params": {"event": {"content": "still running"}},
        },
    )
    assert invocation.status_code == 200
    assert invocation.json()["result"]["actions"][0]["content"] == ("stable:still running")

    async with app.state.database.session_factory() as session:
        deployment = await session.get(
            PluginDeployment,
            UUID(stable["deployment_id"]),
        )
        candidate_activation_count = await session.scalar(
            select(func.count(PluginRevisionActivation.id)).where(
                PluginRevisionActivation.revision_id == UUID(candidate.json()["id"])
            )
        )
        active_rows = list(
            await session.scalars(
                select(PluginRevisionActivation).where(
                    PluginRevisionActivation.deployment_id == UUID(stable["deployment_id"]),
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                )
            )
        )
    assert deployment is not None
    assert deployment.status == PluginDeploymentStatus.RUNNING
    assert str(deployment.active_revision_id) == stable["revision_id"]
    assert candidate_activation_count == 0
    assert len(active_rows) == 1
    assert str(active_rows[0].revision_id) == stable["revision_id"]


@pytest.mark.parametrize("scope_key", SCOPE_FILTER_KEYS)
def test_scope_overlap_requires_provable_disjoint_filter(scope_key: str) -> None:
    assert not _scopes_overlap(
        {scope_key: ["left"]},
        {scope_key: ["right"]},
    )
    assert not _scopes_overlap(
        {scope_key: []},
        {scope_key: ["right"]},
    )
    assert _scopes_overlap(
        {scope_key: ["same"]},
        {scope_key: ["same"]},
    )
    assert _scopes_overlap(
        {scope_key: ["left"]},
        {},
    )
    assert _scopes_overlap(
        {scope_key: ["left"]},
        {scope_key: "right"},
    )


async def _install_echo(client: AsyncClient) -> tuple[str, str, str]:
    connection = await client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    assert connection.status_code == 201
    workspace_id = connection.json()["workspace_id"]
    installed = await client.post(
        "/api/v1/plugins/builtins/builtin.echo/install",
        json={"workspace_id": workspace_id},
    )
    assert installed.status_code == 201
    return (
        workspace_id,
        installed.json()["plugin"]["id"],
        installed.json()["package"]["id"],
    )


async def _create_echo_deployment(
    client: AsyncClient,
    *,
    workspace_id: str,
    plugin_id: str,
    package_id: str,
    name: str,
    scope: dict[str, object],
    prefix: str = "",
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/plugins/deployments",
        json={
            "workspace_id": workspace_id,
            "plugin_id": plugin_id,
            "package_version_id": package_id,
            "name": name,
            "config": {"prefix": prefix},
            "scope": scope,
            "grants": ["message.reply.text"],
        },
    )
    assert response.status_code == 201, response.text
    return {
        "deployment_id": response.json()["deployment"]["id"],
        "revision_id": response.json()["revision"]["id"],
    }


async def _activate(client: AsyncClient, deployment: dict[str, str]) -> Response:
    return await client.post(
        f"/api/v1/plugins/deployments/{deployment['deployment_id']}/revisions/"
        f"{deployment['revision_id']}/activate"
    )


async def _assert_echo_result(
    client: AsyncClient,
    deployment_id: str,
    expected: str,
) -> None:
    invocation = await client.post(
        f"/api/v1/plugins/deployments/{deployment_id}/invoke",
        json={
            "method": "handle_event",
            "params": {"event": {"content": expected.split(":", 1)[1]}},
        },
    )
    assert invocation.status_code == 200
    assert invocation.json()["result"]["actions"][0]["content"] == expected
