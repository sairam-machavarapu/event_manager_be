"""Private saved events, organiser subscriptions and ticket waitlists."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    for name, target, column in (
        ("saved_events", "events", "event_id"),
        ("organizer_follows", "organizers", "organizer_id"),
        ("waitlist_entries", "ticket_types", "ticket_type_id"),
    ):
        extra = []
        if name == "waitlist_entries":
            extra = [
                sa.Column("status", sa.String(20), nullable=False, server_default="waiting"),
                sa.Column(
                    "next_check_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.current_timestamp(),
                ),
                sa.CheckConstraint("status IN ('waiting','notified')", name="status"),
            ]
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column(column, sa.Uuid(), sa.ForeignKey(f"{target}.id"), nullable=False),
            sa.UniqueConstraint("user_id", column),
            *extra,
        )
        op.create_index(f"ix_{name}_user_id", name, ["user_id"])
        op.create_index(f"ix_{name}_{column}", name, [column])
    op.create_index(
        "ix_waitlist_entries_due", "waitlist_entries", ["status", "next_check_at", "id"]
    )
    op.create_index("ix_waitlist_entries_next_check_at", "waitlist_entries", ["next_check_at"])


def downgrade():
    for name in ("waitlist_entries", "organizer_follows", "saved_events"):
        op.drop_table(name)
