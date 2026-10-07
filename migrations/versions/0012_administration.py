"""Event reports and administration reasons."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("audit_entries") as batch:
        batch.add_column(sa.Column("reason", sa.String(1000), nullable=True))
    op.create_table(
        "event_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("organizer_id", sa.Uuid(), sa.ForeignKey("organizers.id"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("reporter_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(1000)),
        sa.Column("resolved_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.CheckConstraint("status IN ('open','resolved','dismissed')", name="status"),
    )
    op.create_index("ix_event_reports_organizer_id", "event_reports", ["organizer_id"])
    op.create_index("ix_event_reports_event_id", "event_reports", ["event_id"])


def downgrade():
    op.drop_table("event_reports")
    with op.batch_alter_table("audit_entries") as batch:
        batch.drop_column("reason")
