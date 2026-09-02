"""remove the legacy social bridge and its call ledger

Revision ID: c3a9f1e2d4b6
Revises: b7f2d9a41c6e
Create Date: 2026-09-02 16:00:00.000000

This migration intentionally destroys integration-specific state. The removed
feature is no longer part of the application, so restoring its empty schema on
downgrade would create tables that no supported code owns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a9f1e2d4b6"
down_revision: str | None = "b7f2d9a41c6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS tool_call"))
    op.execute(sa.text("DROP TABLE IF EXISTS maibot_bridge_envelope"))
    op.execute(sa.text("DROP TABLE IF EXISTS maibot_connection_state"))
    op.execute(
        sa.text("DELETE FROM plugin WHERE plugin_id = :plugin_id").bindparams(
            plugin_id="builtin.maibot-connector"
        )
    )
    op.execute(
        sa.text("DELETE FROM rbac_permission WHERE code = :code").bindparams(code="tool.read")
    )


def downgrade() -> None:
    raise RuntimeError("the legacy social bridge removal is irreversible")
