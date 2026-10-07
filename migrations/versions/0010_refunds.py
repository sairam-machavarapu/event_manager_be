"""Durable full refund intents."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("provider_reference", sa.String(40), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_refunds"),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempts.id"],
            name="fk_refunds_payment_attempt_id_payment_attempts",
        ),
        sa.UniqueConstraint("payment_attempt_id", name="uq_refunds_payment_attempt_id"),
        sa.UniqueConstraint("provider_reference", name="uq_refunds_provider_reference"),
        sa.CheckConstraint("amount_minor > 0 AND currency = 'INR'", name="ck_refunds_amount"),
        sa.CheckConstraint(
            "status IN ('queued','pending','succeeded','failed')", name="ck_refunds_status"
        ),
        sa.CheckConstraint(
            "reason IN ('event_cancelled','reservation_unavailable')", name="ck_refunds_reason"
        ),
    )


def downgrade():
    op.drop_table("refunds")
