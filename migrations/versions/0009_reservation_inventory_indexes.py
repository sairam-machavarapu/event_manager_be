"""Indexes for live reservation inventory."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_bookings_inventory", "bookings", ["event_id", "status", "expires_at"])
    op.create_index("ix_booking_items_inventory", "booking_items", ["ticket_type_id", "booking_id"])


def downgrade():
    op.drop_index("ix_booking_items_inventory", table_name="booking_items")
    op.drop_index("ix_bookings_inventory", table_name="bookings")
