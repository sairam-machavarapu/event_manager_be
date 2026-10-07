"""Durable payment recovery and authenticated webhook receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "payment_attempts",
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index("ix_payment_attempts_next_check_at", "payment_attempts", ["next_check_at"])
    op.create_table(
        "webhook_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("webhook_receipts")
    op.drop_index("ix_payment_attempts_next_check_at", table_name="payment_attempts")
    op.drop_column("payment_attempts", "next_check_at")
