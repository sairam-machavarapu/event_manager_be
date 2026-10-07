"""Durable discovery cache invalidation generation."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    version = op.create_table(
        "discovery_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name="singleton"),
    )
    op.bulk_insert(version, [{"id": 1, "revision": 0}])


def downgrade():
    op.drop_table("discovery_version")
