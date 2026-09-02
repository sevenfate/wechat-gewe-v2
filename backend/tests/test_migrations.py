from __future__ import annotations

import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from wechat_bot.db.base import Base
from wechat_bot.db.registry import load_all_models

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _migration_config(database_path: Path) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}"
    return config


def _sqlite_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row[0]) for row in rows}


def test_initial_migration_round_trip_matches_registered_metadata(tmp_path: Path) -> None:
    load_all_models()
    expected_tables = set(Base.metadata.tables)
    assert len(expected_tables) == 36

    database_path = tmp_path / "migration-round-trip.db"
    config = _migration_config(database_path)

    command.upgrade(config, "head")
    assert expected_tables <= _sqlite_tables(database_path)
    command.check(config)


def test_initial_migration_contains_security_critical_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-columns.db"
    config = _migration_config(database_path)
    command.upgrade(config, "head")

    expected_columns = {
        "workspace": {"singleton_key"},
        "gewe_connection": {
            "token_ciphertext",
            "callback_secret_ciphertext",
            "callback_secret_hash",
            "callback_expected_url_ciphertext",
        },
        "bot_account": {
            "pending_login_uuid",
            "qr_expires_at",
            "last_status_checked_at",
            "last_status_error",
            "status",
        },
        "admin_session": {
            "token_hash",
            "csrf_token_hash",
            "idle_expires_at",
            "absolute_expires_at",
            "auth_version",
            "revoked_at",
        },
        "auth_bootstrap_state": {"owner_user_id", "token_fingerprint", "consumed_at"},
        "webhook_inbox": {"dedup_key", "payload_sha256", "raw_payload", "trace_id"},
        "outbox_message": {
            "authorization_context",
            "last_attempt_started_at",
            "last_attempt_finished_at",
            "provider_message_id",
            "provider_new_message_id",
            "provider_create_time",
            "provider_message_type",
        },
        "plugin_event_dispatch": {
            "event_id",
            "deployment_id",
            "revision_id",
            "status",
            "attempt_count",
            "accepted_action_count",
            "last_error_type",
        },
        "agent_session": {
            "agent_version_id",
            "requester_principal_id",
            "task_scope",
            "task_scope_sha256",
            "last_inbox_seq",
            "last_event_seq",
        },
        "agent_run": {
            "session_id",
            "idempotency_key",
            "input_payload",
            "input_sha256",
            "status",
            "active_slot",
        },
        "agent_pending_question": {
            "allowed_principal_id",
            "status",
            "expires_at",
            "answer_payload",
            "answer_inbox_seq",
        },
        "agent_event": {"session_id", "seq", "event_type", "payload"},
        "agent_session_inbox": {
            "session_id",
            "seq",
            "question_id",
            "payload_sha256",
        },
    }

    with sqlite3.connect(database_path) as connection:
        for table_name, required_columns in expected_columns.items():
            rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            actual_columns = {str(row[1]) for row in rows}
            assert required_columns <= actual_columns


def test_initial_migration_renders_for_postgresql() -> None:
    output = StringIO()
    config = Config(str(BACKEND_ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://migration.invalid/wechat_bot"

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert 'CREATE TABLE "workspace"' in sql or "CREATE TABLE workspace" in sql
    assert (
        'CREATE TABLE "auth_bootstrap_state"' in sql or "CREATE TABLE auth_bootstrap_state" in sql
    )
    assert "callback_secret_ciphertext BYTEA NOT NULL" in sql
    assert "JSONB" in sql
    assert 'CREATE TABLE "agent_run"' in sql or "CREATE TABLE agent_run" in sql
    assert "uq_agent_run_session_active" in sql
    assert "uq_agent_run_session_idempotency" in sql
    assert (
        'CREATE TABLE "plugin_event_dispatch"' in sql or "CREATE TABLE plugin_event_dispatch" in sql
    )
    assert "provider_message_id VARCHAR(255)" in sql


def test_legacy_social_bridge_tables_are_removed(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-social-bridge.db"
    config = _migration_config(database_path)
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}"

    command.upgrade(config, "b7f2d9a41c6e")
    legacy_tables = {"maibot_bridge_envelope", "maibot_connection_state", "tool_call"}
    with sqlite3.connect(database_path) as connection:
        for table_name in legacy_tables:
            connection.execute(f'CREATE TABLE "{table_name}" (id TEXT PRIMARY KEY)')
    assert legacy_tables <= _sqlite_tables(database_path)

    command.upgrade(config, "head")
    assert legacy_tables.isdisjoint(_sqlite_tables(database_path))


def test_single_workspace_migration_fails_closed_for_legacy_multi_workspace(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-multi-workspace.db"
    config = _migration_config(database_path)
    command.upgrade(config, "f2ed1f640367")
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO workspace (id, name, slug) VALUES (?, ?, ?)",
            [
                ("1" * 32, "Workspace One", "workspace-one"),
                ("2" * 32, "Workspace Two", "workspace-two"),
            ],
        )

    with pytest.raises(IntegrityError):
        command.upgrade(config, "head")
