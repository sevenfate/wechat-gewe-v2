from __future__ import annotations

import hashlib
import json
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import Workspace
from wechat_bot.db.plugin_models import (
    Plugin,
    PluginActivationStatus,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginPackageStatus,
    PluginPackageVersion,
    PluginRevisionActivation,
)
from wechat_bot.maibot.constants import MAIBOT_CONNECTOR_PLUGIN_ID
from wechat_bot.plugins.manifest import PluginManifest, load_plugin_manifest
from wechat_bot.plugins.schemas import PluginDeploymentCreate, PluginRevisionCreate
from wechat_bot.plugins.supervisor import (
    PluginActivationPreparation,
    PluginDeactivationPreparation,
    PluginLaunchSpec,
    PluginRuntimeError,
    PluginSupervisor,
)
from wechat_bot.policy.fence import lock_authorization_fence

_INSTALLED_BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "builtin_plugins"
_SOURCE_BUILTIN_ROOT = Path(__file__).resolve().parents[3] / "builtin_plugins"
_BUILTIN_ROOT = (
    _INSTALLED_BUILTIN_ROOT if _INSTALLED_BUILTIN_ROOT.is_dir() else _SOURCE_BUILTIN_ROOT
)
BUILTIN_PACKAGES = {
    "builtin.echo": _BUILTIN_ROOT / "echo",
    "builtin.weather": _BUILTIN_ROOT / "weather",
    MAIBOT_CONNECTOR_PLUGIN_ID: _BUILTIN_ROOT / "maibot_connector",
}
SCOPE_FILTER_KEYS = (
    "bot_account_ids",
    "chatroom_ids",
    "contact_ids",
    "conversation_ids",
)
PLUGIN_REDACTION_MARKER = "__WECHAT_BOT_SECRET_RETAINED__"
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey",
        "apisecret",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "signingkey",
        "token",
    }
)
_SCHEMA_COMBINERS = ("allOf", "anyOf", "oneOf")
_MISSING = object()


class PluginCatalogError(ValueError):
    pass


class PluginObjectNotFoundError(LookupError):
    pass


class PluginActivationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PluginRestoreResult:
    restored_deployment_ids: tuple[UUID, ...]
    failed_deployment_ids: tuple[UUID, ...]
    runtime_activations: tuple[PluginActivationPreparation, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginActivationTransition:
    deployment: PluginDeployment
    activation: PluginRevisionActivation
    runtime: PluginActivationPreparation


@dataclass(frozen=True, slots=True)
class PluginDeactivationTransition:
    deployment: PluginDeployment
    runtime: PluginDeactivationPreparation


class PluginCatalogService:
    def __init__(self, cipher: CredentialCipher) -> None:
        self._cipher = cipher

    async def workspace_context(self, session: AsyncSession) -> Workspace:
        workspaces = list(await session.scalars(select(Workspace).order_by(Workspace.id).limit(2)))
        if not workspaces:
            raise PluginObjectNotFoundError("workspace not found")
        if len(workspaces) > 1:
            raise PluginCatalogError("this release supports exactly one workspace")
        return workspaces[0]

    async def install_builtin(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        plugin_id: str,
    ) -> tuple[Plugin, PluginPackageVersion]:
        if await session.get(Workspace, workspace_id) is None:
            raise PluginObjectNotFoundError("workspace not found")
        package_path = BUILTIN_PACKAGES.get(plugin_id)
        if package_path is None:
            raise PluginObjectNotFoundError("builtin plugin not found")
        manifest, package_sha256 = load_plugin_manifest(package_path)
        plugin = await session.scalar(
            select(Plugin).where(
                Plugin.workspace_id == workspace_id,
                Plugin.plugin_id == manifest.plugin_id,
            )
        )
        if plugin is None:
            plugin = Plugin(
                workspace_id=workspace_id,
                plugin_id=manifest.plugin_id,
                name=manifest.name,
                description=manifest.description,
            )
            session.add(plugin)
            await session.flush()
        elif plugin.retired_at is not None:
            raise PluginCatalogError("retired plugin cannot receive new packages")

        package = await session.scalar(
            select(PluginPackageVersion).where(
                PluginPackageVersion.plugin_id == plugin.id,
                PluginPackageVersion.semantic_version == manifest.version,
            )
        )
        if package is not None:
            if package.package_sha256 != package_sha256:
                raise PluginCatalogError(
                    "the same plugin version already exists with different content"
                )
            return plugin, package

        package = PluginPackageVersion(
            plugin_id=plugin.id,
            semantic_version=manifest.version,
            package_sha256=package_sha256,
            manifest=manifest.model_dump(by_alias=True, mode="json"),
            package_path=str(package_path.resolve(strict=True)),
            status=PluginPackageStatus.AVAILABLE,
        )
        session.add(package)
        await session.flush()
        return plugin, package

    async def create_deployment(
        self,
        session: AsyncSession,
        payload: PluginDeploymentCreate,
    ) -> tuple[PluginDeployment, PluginDeploymentRevision]:
        plugin, package = await self._plugin_and_package(
            session,
            workspace_id=payload.workspace_id,
            plugin_id=payload.plugin_id,
            package_version_id=payload.package_version_id,
        )
        existing = await session.scalar(
            select(PluginDeployment.id).where(
                PluginDeployment.workspace_id == payload.workspace_id,
                PluginDeployment.name == payload.name,
            )
        )
        if existing is not None:
            raise PluginCatalogError("deployment name already exists")
        deployment = PluginDeployment(
            workspace_id=payload.workspace_id,
            plugin_id=plugin.id,
            name=payload.name.strip(),
            status=PluginDeploymentStatus.STOPPED,
        )
        session.add(deployment)
        await session.flush()
        revision = await self._create_revision(
            session,
            deployment=deployment,
            package=package,
            config=payload.config,
            scope=payload.scope,
            grants=payload.grants,
        )
        return deployment, revision

    async def create_revision(
        self,
        session: AsyncSession,
        deployment_id: UUID,
        payload: PluginRevisionCreate,
    ) -> PluginDeploymentRevision:
        deployment = await session.get(PluginDeployment, deployment_id)
        if deployment is None:
            raise PluginObjectNotFoundError("plugin deployment not found")

        source_revision: PluginDeploymentRevision | None = None
        source_config: dict[str, Any] | None = None
        if payload.source_revision_id is not None:
            source_revision = await session.get(
                PluginDeploymentRevision,
                payload.source_revision_id,
            )
            if source_revision is None or source_revision.deployment_id != deployment_id:
                raise PluginObjectNotFoundError("source plugin deployment revision not found")
            source_config = self._decrypt_revision_config(source_revision)

        package_version_id = payload.package_version_id
        if package_version_id is None and source_revision is not None:
            package_version_id = source_revision.package_version_id
        if package_version_id is None:
            raise PluginCatalogError(
                "package_version_id is required when source_revision_id is not provided"
            )
        package = await session.get(PluginPackageVersion, package_version_id)
        if package is None or package.status is not PluginPackageStatus.AVAILABLE:
            raise PluginObjectNotFoundError("available plugin package not found")
        if package.plugin_id != deployment.plugin_id:
            raise PluginCatalogError("package belongs to a different plugin")

        if source_revision is None:
            config = payload.config or {}
            if _contains_secret_placeholder(config):
                raise PluginCatalogError("secret placeholder requires a source revision")
            scope = payload.scope or {}
            grants = payload.grants or []
        else:
            assert source_config is not None
            config = (
                deepcopy(source_config)
                if payload.config is None
                else _restore_secret_placeholders(
                    _merge_revision_config(source_config, payload.config),
                    source_config,
                )
            )
            scope = deepcopy(source_revision.scope) if payload.scope is None else payload.scope
            grants = list(source_revision.grants) if payload.grants is None else payload.grants
        return await self._create_revision(
            session,
            deployment=deployment,
            package=package,
            config=config,
            scope=scope,
            grants=grants,
        )

    async def revision_draft(
        self,
        session: AsyncSession,
        *,
        deployment_id: UUID,
        revision_id: UUID,
    ) -> tuple[PluginDeploymentRevision, dict[str, Any]]:
        deployment = await session.get(PluginDeployment, deployment_id)
        revision = await session.get(PluginDeploymentRevision, revision_id)
        if deployment is None or revision is None or revision.deployment_id != deployment_id:
            raise PluginObjectNotFoundError("plugin deployment revision not found")
        package = await session.get(PluginPackageVersion, revision.package_version_id)
        if package is None or package.plugin_id != deployment.plugin_id:
            raise PluginObjectNotFoundError("plugin package not found")
        manifest = PluginManifest.model_validate(package.manifest)
        config = self._decrypt_revision_config(revision)
        return revision, _redact_sensitive_config(config, manifest.config_schema)

    async def activate_revision(
        self,
        session: AsyncSession,
        *,
        supervisor: PluginSupervisor,
        deployment_id: UUID,
        revision_id: UUID,
    ) -> PluginActivationTransition:
        deployment = await session.get(PluginDeployment, deployment_id)
        revision = await session.get(PluginDeploymentRevision, revision_id)
        if deployment is None or revision is None or revision.deployment_id != deployment_id:
            raise PluginObjectNotFoundError("plugin deployment revision not found")
        package = await session.get(PluginPackageVersion, revision.package_version_id)
        if package is None or package.status is not PluginPackageStatus.AVAILABLE:
            raise PluginObjectNotFoundError("available plugin package not found")

        await lock_authorization_fence(
            session,
            deployment.workspace_id,
            shared=False,
        )
        await session.scalar(
            select(Workspace.id).where(Workspace.id == deployment.workspace_id).with_for_update()
        )
        locked_deployment = await session.scalar(
            select(PluginDeployment).where(PluginDeployment.id == deployment_id).with_for_update()
        )
        if locked_deployment is None:
            raise PluginObjectNotFoundError("plugin deployment not found")
        deployment = locked_deployment
        await self._reject_running_command_conflicts(
            session,
            deployment=deployment,
            revision=revision,
            package=package,
        )

        current_max = await session.scalar(
            select(func.max(PluginRevisionActivation.activation_epoch)).where(
                PluginRevisionActivation.deployment_id == deployment_id
            )
        )
        next_epoch = (current_max or 0) + 1
        activation = PluginRevisionActivation(
            deployment_id=deployment_id,
            revision_id=revision_id,
            activation_epoch=next_epoch,
            fencing_token=secrets.token_hex(32),
            status=PluginActivationStatus.STARTING,
        )
        session.add(activation)
        deployment.status = PluginDeploymentStatus.STARTING
        await session.flush()

        preparation: PluginActivationPreparation | None = None
        try:
            spec = self._launch_spec(package, revision)
            preparation = await supervisor.prepare_activation(
                str(deployment_id),
                spec,
                requested_epoch=next_epoch,
                activation_id=str(activation.id),
                fencing_token=activation.fencing_token,
            )
        except (PluginRuntimeError, ValueError) as exc:
            activation.status = PluginActivationStatus.FAILED
            activation.error_detail = str(exc)[:500]
            if deployment.active_revision_id is None:
                deployment.status = PluginDeploymentStatus.FAILED
            else:
                deployment.status = PluginDeploymentStatus.RUNNING
            deployment.last_error = "candidate activation failed"
            await session.flush()
            raise PluginActivationError("candidate plugin activation failed") from exc

        try:
            active_rows = list(
                await session.scalars(
                    select(PluginRevisionActivation).where(
                        PluginRevisionActivation.deployment_id == deployment_id,
                        PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                        PluginRevisionActivation.id != activation.id,
                    )
                )
            )
            for old in active_rows:
                old.status = PluginActivationStatus.STOPPED
                old.stopped_at = utc_now()
            activation.activation_epoch = preparation.activation_epoch
            activation.status = PluginActivationStatus.ACTIVE
            activation.started_at = utc_now()
            deployment.active_revision_id = revision.id
            deployment.status = PluginDeploymentStatus.RUNNING
            deployment.last_error = None
            await session.flush()
        except BaseException:
            await supervisor.abort_activation(preparation)
            raise
        return PluginActivationTransition(deployment, activation, preparation)

    async def deactivate(
        self,
        session: AsyncSession,
        *,
        supervisor: PluginSupervisor,
        deployment_id: UUID,
    ) -> PluginDeactivationTransition:
        deployment = await session.scalar(
            select(PluginDeployment).where(PluginDeployment.id == deployment_id).with_for_update()
        )
        if deployment is None:
            raise PluginObjectNotFoundError("plugin deployment not found")
        await lock_authorization_fence(
            session,
            deployment.workspace_id,
            shared=False,
        )
        try:
            preparation = await supervisor.prepare_deactivation(str(deployment_id))
        except PluginRuntimeError as exc:
            raise PluginActivationError("plugin deactivation is already in progress") from exc
        try:
            deployment.status = PluginDeploymentStatus.DRAINING
            await session.flush()
            active_rows = list(
                await session.scalars(
                    select(PluginRevisionActivation).where(
                        PluginRevisionActivation.deployment_id == deployment_id,
                        PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                    )
                )
            )
            for activation in active_rows:
                activation.status = PluginActivationStatus.STOPPED
                activation.stopped_at = utc_now()
            deployment.active_revision_id = None
            deployment.status = PluginDeploymentStatus.STOPPED
            await session.flush()
        except BaseException:
            await supervisor.abort_deactivation(preparation)
            raise
        return PluginDeactivationTransition(deployment, preparation)

    async def restore_active_deployments(
        self,
        session: AsyncSession,
        *,
        supervisor: PluginSupervisor,
    ) -> PluginRestoreResult:
        deployments = list(
            await session.scalars(
                select(PluginDeployment)
                .where(
                    PluginDeployment.status == PluginDeploymentStatus.RUNNING,
                    PluginDeployment.active_revision_id.is_not(None),
                )
                .order_by(PluginDeployment.created_at, PluginDeployment.id)
            )
        )
        restored: list[UUID] = []
        failed: list[UUID] = []
        runtime_activations: list[PluginActivationPreparation] = []
        for deployment in deployments:
            revision_id = deployment.active_revision_id
            if revision_id is None:
                continue
            try:
                transition = await self.activate_revision(
                    session,
                    supervisor=supervisor,
                    deployment_id=deployment.id,
                    revision_id=revision_id,
                )
            except PluginActivationError:
                deployment.status = PluginDeploymentStatus.FAILED
                deployment.last_error = "startup activation restore failed"
                failed.append(deployment.id)
            else:
                restored.append(deployment.id)
                runtime_activations.append(transition.runtime)
        await session.flush()
        return PluginRestoreResult(
            tuple(restored),
            tuple(failed),
            tuple(runtime_activations),
        )

    async def catalog(
        self,
        session: AsyncSession,
    ) -> tuple[
        list[Plugin],
        list[PluginPackageVersion],
        list[PluginDeployment],
        list[PluginDeploymentRevision],
    ]:
        plugins = list(await session.scalars(select(Plugin).order_by(Plugin.plugin_id)))
        packages = list(
            await session.scalars(
                select(PluginPackageVersion).order_by(PluginPackageVersion.created_at.desc())
            )
        )
        deployments = list(
            await session.scalars(select(PluginDeployment).order_by(PluginDeployment.name))
        )
        revisions = list(
            await session.scalars(
                select(PluginDeploymentRevision).order_by(
                    PluginDeploymentRevision.deployment_id,
                    PluginDeploymentRevision.revision_number.desc(),
                )
            )
        )
        return plugins, packages, deployments, revisions

    async def _create_revision(
        self,
        session: AsyncSession,
        *,
        deployment: PluginDeployment,
        package: PluginPackageVersion,
        config: dict[str, Any],
        scope: dict[str, Any],
        grants: list[str],
    ) -> PluginDeploymentRevision:
        manifest = PluginManifest.model_validate(package.manifest)
        self._validate_config(manifest, config)
        undeclared_grants = set(grants) - set(manifest.capabilities)
        if undeclared_grants:
            raise PluginCatalogError("deployment grants exceed manifest capabilities")
        validated_scope = _validate_scope(scope, workspace_id=deployment.workspace_id)
        canonical_config = _canonical_json(config)
        canonical_scope = _canonical_json(validated_scope)
        ciphertext = self._cipher.encrypt(canonical_config)
        config_fingerprint = hashlib.sha256(ciphertext).hexdigest()
        content_sha256 = hashlib.sha256(
            b"\0".join(
                (
                    package.package_sha256.encode("ascii"),
                    config_fingerprint.encode("ascii"),
                    canonical_scope.encode("utf-8"),
                    _canonical_json(sorted(set(grants))).encode("utf-8"),
                )
            )
        ).hexdigest()
        current_max = await session.scalar(
            select(func.max(PluginDeploymentRevision.revision_number)).where(
                PluginDeploymentRevision.deployment_id == deployment.id
            )
        )
        revision = PluginDeploymentRevision(
            deployment_id=deployment.id,
            package_version_id=package.id,
            revision_number=(current_max or 0) + 1,
            config_ciphertext=ciphertext,
            config_fingerprint=config_fingerprint,
            scope=validated_scope,
            grants=list(dict.fromkeys(grants)),
            content_sha256=content_sha256,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def _plugin_and_package(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        plugin_id: UUID,
        package_version_id: UUID,
    ) -> tuple[Plugin, PluginPackageVersion]:
        plugin = await session.get(Plugin, plugin_id)
        package = await session.get(PluginPackageVersion, package_version_id)
        if plugin is None or plugin.workspace_id != workspace_id:
            raise PluginObjectNotFoundError("plugin not found in workspace")
        if (
            package is None
            or package.plugin_id != plugin.id
            or package.status is not PluginPackageStatus.AVAILABLE
        ):
            raise PluginObjectNotFoundError("available plugin package not found")
        return plugin, package

    def _launch_spec(
        self,
        package: PluginPackageVersion,
        revision: PluginDeploymentRevision,
    ) -> PluginLaunchSpec:
        package_path = Path(package.package_path)
        manifest, actual_hash = load_plugin_manifest(package_path)
        if actual_hash != package.package_sha256:
            raise PluginCatalogError("plugin package changed after verification")
        config = self._decrypt_revision_config(revision)
        return PluginLaunchSpec(
            package_path=package_path,
            manifest=manifest,
            package_sha256=actual_hash,
            config=config,
        )

    def _decrypt_revision_config(
        self,
        revision: PluginDeploymentRevision,
    ) -> dict[str, Any]:
        try:
            config = json.loads(self._cipher.decrypt(revision.config_ciphertext))
        except (TypeError, ValueError) as exc:
            raise PluginCatalogError("stored plugin configuration is invalid") from exc
        if not isinstance(config, dict):
            raise PluginCatalogError("stored plugin configuration is not an object")
        return config

    @staticmethod
    def _validate_config(manifest: PluginManifest, config: dict[str, Any]) -> None:
        try:
            validator = Draft202012Validator(manifest.config_schema)
            errors = sorted(validator.iter_errors(config), key=lambda error: list(error.path))
        except SchemaError as exc:
            raise PluginCatalogError("plugin config schema is invalid") from exc
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path)
            location = f" at $.{path}" if path else ""
            raise PluginCatalogError(
                f"plugin configuration is invalid{location} ({error.validator})"
            )

    @staticmethod
    async def _reject_running_command_conflicts(
        session: AsyncSession,
        *,
        deployment: PluginDeployment,
        revision: PluginDeploymentRevision,
        package: PluginPackageVersion,
    ) -> None:
        candidate_manifest = PluginManifest.model_validate(package.manifest)
        candidate_commands = _command_names(candidate_manifest)
        if not candidate_commands:
            return

        running_rows = (
            await session.execute(
                select(
                    PluginDeployment,
                    PluginDeploymentRevision,
                    PluginPackageVersion,
                )
                .join(
                    PluginDeploymentRevision,
                    PluginDeploymentRevision.id == PluginDeployment.active_revision_id,
                )
                .join(
                    PluginPackageVersion,
                    PluginPackageVersion.id == PluginDeploymentRevision.package_version_id,
                )
                .where(
                    PluginDeployment.workspace_id == deployment.workspace_id,
                    PluginDeployment.status == PluginDeploymentStatus.RUNNING,
                    PluginDeployment.id != deployment.id,
                )
                .order_by(PluginDeployment.created_at, PluginDeployment.id)
            )
        ).all()
        for running_deployment, running_revision, running_package in running_rows:
            if not _scopes_overlap(revision.scope, running_revision.scope):
                continue
            running_manifest = PluginManifest.model_validate(running_package.manifest)
            conflicts = candidate_commands & _command_names(running_manifest)
            if conflicts:
                conflict = min(conflicts)
                raise PluginActivationError(
                    "plugin command "
                    f"'{conflict}' conflicts with running deployment "
                    f"'{running_deployment.name}' in an overlapping scope"
                )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PluginCatalogError("plugin data must be valid JSON") from exc


def _redact_sensitive_config(
    config: dict[str, Any],
    schema: dict[str, object],
) -> dict[str, Any]:
    redacted = _redact_sensitive_value(config, (schema,))
    if not isinstance(redacted, dict):
        raise PluginCatalogError("plugin configuration is not an object")
    return redacted


def _redact_sensitive_value(
    value: Any,
    schemas: tuple[dict[str, Any], ...],
    *,
    field_name: str | None = None,
) -> Any:
    if _is_sensitive_field(field_name, schemas):
        return PLUGIN_REDACTION_MARKER
    if isinstance(value, dict):
        return {
            key: _redact_sensitive_value(
                item,
                _child_schemas(schemas, key),
                field_name=key,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        item_schemas = _item_schemas(schemas)
        return [
            _redact_sensitive_value(item, item_schemas, field_name=field_name) for item in value
        ]
    return deepcopy(value)


def _is_sensitive_field(
    field_name: str | None,
    schemas: tuple[dict[str, Any], ...],
) -> bool:
    if field_name is not None:
        canonical_name = re.sub(r"[^a-z0-9]", "", field_name.casefold())
        if canonical_name in _SECRET_FIELD_NAMES:
            return True
    for schema in schemas:
        for variant in _schema_variants(schema):
            if variant.get("writeOnly") is True or variant.get("x-sensitive") is True:
                return True
            field_format = variant.get("format")
            if isinstance(field_format, str) and field_format.casefold() == "password":
                return True
    return False


def _schema_variants(schema: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    variants = [schema]
    for keyword in _SCHEMA_COMBINERS:
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, dict):
                variants.extend(_schema_variants(branch))
    for keyword in ("if", "then", "else"):
        branch = schema.get(keyword)
        if isinstance(branch, dict):
            variants.extend(_schema_variants(branch))
    return tuple(variants)


def _child_schemas(
    schemas: tuple[dict[str, Any], ...],
    key: str,
) -> tuple[dict[str, Any], ...]:
    children: list[dict[str, Any]] = []
    for schema in schemas:
        for variant in _schema_variants(schema):
            properties = variant.get("properties")
            if isinstance(properties, dict):
                child = properties.get(key)
                if isinstance(child, dict):
                    children.append(child)
            patterns = variant.get("patternProperties")
            if isinstance(patterns, dict):
                for pattern, child in patterns.items():
                    if not isinstance(pattern, str) or not isinstance(child, dict):
                        continue
                    try:
                        matches = re.search(pattern, key) is not None
                    except re.error:
                        matches = False
                    if matches:
                        children.append(child)
            additional = variant.get("additionalProperties")
            if isinstance(additional, dict):
                children.append(additional)
    return tuple(children)


def _item_schemas(
    schemas: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for schema in schemas:
        for variant in _schema_variants(schema):
            item_schema = variant.get("items")
            if isinstance(item_schema, dict):
                items.append(item_schema)
            prefix_items = variant.get("prefixItems")
            if isinstance(prefix_items, list):
                items.extend(item for item in prefix_items if isinstance(item, dict))
    return tuple(items)


def _restore_secret_placeholders(candidate: Any, source: Any = _MISSING) -> Any:
    if candidate == PLUGIN_REDACTION_MARKER:
        if source is _MISSING:
            raise PluginCatalogError("secret placeholder has no value in the source revision")
        return deepcopy(source)
    if isinstance(candidate, dict):
        source_dict = source if isinstance(source, dict) else {}
        return {
            key: _restore_secret_placeholders(value, source_dict.get(key, _MISSING))
            for key, value in candidate.items()
        }
    if isinstance(candidate, list):
        source_list = source if isinstance(source, list) else []
        return [
            _restore_secret_placeholders(
                value,
                source_list[index] if index < len(source_list) else _MISSING,
            )
            for index, value in enumerate(candidate)
        ]
    return deepcopy(candidate)


def _merge_revision_config(source: Any, changes: Any) -> Any:
    if not isinstance(source, dict) or not isinstance(changes, dict):
        return deepcopy(changes)
    merged = deepcopy(source)
    for key, value in changes.items():
        merged[key] = _merge_revision_config(source.get(key, _MISSING), value)
    return merged


def _contains_secret_placeholder(value: Any) -> bool:
    if value == PLUGIN_REDACTION_MARKER:
        return True
    if isinstance(value, dict):
        return any(_contains_secret_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret_placeholder(item) for item in value)
    return False


def _validate_scope(scope: dict[str, Any], *, workspace_id: UUID) -> dict[str, Any]:
    allowed_keys = {"workspace_id", *SCOPE_FILTER_KEYS}
    unknown_keys = set(scope) - allowed_keys
    if unknown_keys:
        raise PluginCatalogError(f"plugin scope contains unknown key: {min(unknown_keys)}")

    validated: dict[str, Any] = {}
    if "workspace_id" in scope:
        raw_workspace_id = scope["workspace_id"]
        if not isinstance(raw_workspace_id, str):
            raise PluginCatalogError("plugin scope workspace_id must be a UUID string")
        try:
            parsed_workspace_id = UUID(raw_workspace_id)
        except ValueError as exc:
            raise PluginCatalogError("plugin scope workspace_id must be a UUID string") from exc
        if parsed_workspace_id != workspace_id:
            raise PluginCatalogError("plugin scope workspace_id does not match deployment")
        validated["workspace_id"] = str(parsed_workspace_id)

    for key in SCOPE_FILTER_KEYS:
        if key not in scope:
            continue
        value = scope[key]
        if not isinstance(value, list):
            raise PluginCatalogError(f"plugin scope {key} must be a list of strings")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise PluginCatalogError(f"plugin scope {key} must contain non-empty strings")
            normalized.append(item.strip())
        validated[key] = sorted(set(normalized))
    return validated


def _command_names(manifest: PluginManifest) -> set[str]:
    return {
        name.casefold()
        for command in manifest.commands
        for name in (command.name, *command.aliases)
    }


def _scopes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in SCOPE_FILTER_KEYS:
        left_filter = _explicit_scope_filter(left, key)
        right_filter = _explicit_scope_filter(right, key)
        if (
            left_filter is not None
            and right_filter is not None
            and left_filter.isdisjoint(right_filter)
        ):
            return False
    return True


def _explicit_scope_filter(scope: dict[str, Any], key: str) -> frozenset[str] | None:
    value = scope.get(key)
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return frozenset(value)
