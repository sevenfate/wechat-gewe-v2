"""add the Tool Bridge call ledger

Revision ID: b7f2d9a41c6e
Revises: 4868c0a12c0f
Create Date: 2026-08-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7f2d9a41c6e"
down_revision: str | None = "4868c0a12c0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_document() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tool_call",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connector_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("connector_revision_id", sa.Uuid(), nullable=False),
        sa.Column("connector_activation_id", sa.Uuid(), nullable=False),
        sa.Column("target_deployment_id", sa.Uuid(), nullable=True),
        sa.Column("target_revision_id", sa.Uuid(), nullable=True),
        sa.Column("target_activation_epoch", sa.Integer(), nullable=True),
        sa.Column("external_tool_call_id", sa.String(length=255), nullable=False),
        sa.Column("connector_context_digest", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=160), nullable=False),
        sa.Column("tool_schema_version", sa.String(length=40), nullable=False),
        sa.Column(
            "invocation_mode",
            sa.Enum(
                "USER_REQUESTED",
                "AUTONOMOUS",
                name="tool_invocation_mode",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("arguments", _json_document(), nullable=False),
        sa.Column("arguments_sha256", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=True),
        sa.Column("bot_account_id", sa.Uuid(), nullable=True),
        sa.Column("chatroom_id", sa.Uuid(), nullable=True),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "AUTHORIZED",
                "EXECUTING",
                "SUCCEEDED",
                "FAILED_RETRYABLE",
                "FAILED_FINAL",
                "DENIED",
                "CANCELLED",
                "UNKNOWN",
                name="tool_call_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("result", _json_document(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_tool_call_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connector_deployment_id"],
            ["plugin_deployment.id"],
            name=op.f("fk_tool_call_connector_deployment_id_plugin_deployment"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connector_revision_id"],
            ["plugin_deployment_revision.id"],
            name=op.f("fk_tool_call_connector_revision_id_plugin_deployment_revision"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connector_activation_id"],
            ["plugin_revision_activation.id"],
            name=op.f("fk_tool_call_connector_activation_id_plugin_revision_activation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_deployment_id"],
            ["plugin_deployment.id"],
            name=op.f("fk_tool_call_target_deployment_id_plugin_deployment"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["plugin_deployment_revision.id"],
            name=op.f("fk_tool_call_target_revision_id_plugin_deployment_revision"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principal.id"],
            name=op.f("fk_tool_call_actor_principal_id_principal"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bot_account_id"],
            ["bot_account.id"],
            name=op.f("fk_tool_call_bot_account_id_bot_account"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["chatroom_id"],
            ["chatroom.id"],
            name=op.f("fk_tool_call_chatroom_id_chatroom"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
            name=op.f("fk_tool_call_contact_id_contact"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_call")),
        sa.UniqueConstraint(
            "connector_revision_id",
            "external_tool_call_id",
            name="uq_tool_call_connector_external_id",
        ),
    )
    with op.batch_alter_table("tool_call", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tool_call_workspace_created", ["workspace_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_tool_call_status_available", ["status", "available_at"], unique=False
        )
        batch_op.create_index("ix_tool_call_trace", ["trace_id", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tool_call", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_call_trace")
        batch_op.drop_index("ix_tool_call_status_available")
        batch_op.drop_index("ix_tool_call_workspace_created")
    op.drop_table("tool_call")
