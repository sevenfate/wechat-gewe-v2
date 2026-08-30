from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
ENTRYPOINT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class PluginCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=80)]
    aliases: list[str] = Field(default_factory=list)
    description: Annotated[str, Field(max_length=300)] = ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("plugin command name cannot be blank")
        return normalized

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("plugin command aliases cannot be blank")
        return normalized


class PluginTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=500)] = ""
    effect_class: Literal["READ_ONLY", "WRITE", "SEND", "GROUP_ADMIN", "UNKNOWN"]
    input_schema: dict[str, object] = Field(default_factory=dict)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(alias="id")
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: str
    core_api: Literal["1"]
    entrypoint: str
    description: Annotated[str, Field(max_length=500)] = ""
    events: list[str] = Field(default_factory=list)
    commands: list[PluginCommand] = Field(default_factory=list)
    tools: list[PluginTool] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10
    config_schema: dict[str, object] = Field(default_factory=dict)

    @field_validator("plugin_id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        if PLUGIN_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("plugin id must be a stable dotted identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if SEMVER_PATTERN.fullmatch(value) is None:
            raise ValueError("plugin version must use semantic versioning")
        return value

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        if ENTRYPOINT_PATTERN.fullmatch(value) is None:
            raise ValueError("entrypoint must use module.path:factory syntax")
        return value

    @field_validator("events", "capabilities")
    @classmethod
    def deduplicate_values(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("manifest list values cannot be blank")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def reject_duplicate_command_names(self) -> Self:
        seen: set[str] = set()
        for command in self.commands:
            for name in (command.name, *command.aliases):
                canonical_name = name.casefold()
                if canonical_name in seen:
                    raise ValueError(
                        "plugin command names and aliases must be unique after Unicode casefold"
                    )
                seen.add(canonical_name)
        return self


class PluginPackageError(ValueError):
    pass


def load_plugin_manifest(
    package_path: Path,
    *,
    max_files: int = 1_000,
    max_bytes: int = 20 * 1024 * 1024,
) -> tuple[PluginManifest, str]:
    root = package_path.resolve(strict=True)
    if not root.is_dir():
        raise PluginPackageError("plugin package path must be a directory")
    manifest_path = root / "manifest.toml"
    if not manifest_path.is_file():
        raise PluginPackageError("plugin package is missing manifest.toml")

    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    if len(files) > max_files:
        raise PluginPackageError("plugin package contains too many files")
    total_size = sum(path.stat().st_size for path in files)
    if total_size > max_bytes:
        raise PluginPackageError("plugin package is too large")

    try:
        manifest_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(manifest_data)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise PluginPackageError("plugin manifest is invalid") from exc

    module_name = manifest.entrypoint.partition(":")[0]
    module_path = root.joinpath(*module_name.split(".")).with_suffix(".py")
    package_module_path = root.joinpath(*module_name.split("."), "__init__.py")
    if not module_path.is_file() and not package_module_path.is_file():
        raise PluginPackageError("plugin entrypoint module does not exist in the package")

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return manifest, digest.hexdigest()
