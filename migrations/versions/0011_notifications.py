"""Notification scheduling, deduplication and delivery operations."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("notification_outbox") as batch:
        batch.add_column(sa.Column("organizer_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("booking_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("topic", sa.String(40), nullable=True))
        batch.add_column(sa.Column("dedupe_key", sa.String(255), nullable=True))
        batch.add_column(sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_error_code", sa.String(40), nullable=True))
        batch.create_foreign_key(
            "fk_notification_outbox_organizer_id_organizers", "organizers", ["organizer_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_notification_outbox_booking_id_bookings", "bookings", ["booking_id"], ["id"]
        )
        batch.create_unique_constraint("uq_notification_outbox_dedupe_key", ["dedupe_key"])
        batch.create_index("ix_notification_outbox_organizer_id", ["organizer_id"])


def downgrade():
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_index("ix_notification_outbox_organizer_id")
        batch.drop_constraint("uq_notification_outbox_dedupe_key", type_="unique")
        batch.drop_constraint("fk_notification_outbox_booking_id_bookings", type_="foreignkey")
        batch.drop_constraint("fk_notification_outbox_organizer_id_organizers", type_="foreignkey")
        for name in (
            "last_error_code",
            "skipped_at",
            "failed_at",
            "dedupe_key",
            "topic",
            "booking_id",
            "organizer_id",
        ):
            batch.drop_column(name)
