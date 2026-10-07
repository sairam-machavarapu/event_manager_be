"""Revision guard for organiser autosave."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("revision", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("events", "revision")
