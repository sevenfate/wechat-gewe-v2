"""preserve a historical revision identifier

Revision ID: b7f2d9a41c6e
Revises: 4868c0a12c0f
Create Date: 2026-08-31 00:00:00.000000

The feature introduced by the original revision has been removed. Keeping the
revision identifier allows existing databases to advance to the cleanup
migration without being stamped or rebuilt.
"""

from collections.abc import Sequence

revision: str = "b7f2d9a41c6e"
down_revision: str | None = "4868c0a12c0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
