"""Bounded public listing and PostgreSQL full-text search indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_events_discovery_order", "events", ["status", "starts_at", "id"])
    op.create_index(
        "ix_events_discovery_category", "events", ["category", "status", "starts_at", "id"]
    )
    op.create_index(
        "ix_ticket_types_discovery_price", "ticket_types", ["event_id", "capacity", "price_minor"]
    )
    op.create_index(
        "ix_event_media_discovery_poster",
        "event_media",
        ["event_id", "kind", "status", "created_at", "id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_events_discovery_city",
            "events",
            [sa.text("lower(city)"), "status", "starts_at", "id"],
        )
        op.execute(
            "CREATE INDEX ix_events_discovery_search ON events USING gin "
            "(to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(description, '')"
            " || ' ' || coalesce(venue, '') || ' ' || coalesce(city, '')))"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_events_discovery_search", table_name="events")
        op.drop_index("ix_events_discovery_city", table_name="events")
    op.drop_index("ix_event_media_discovery_poster", table_name="event_media")
    op.drop_index("ix_ticket_types_discovery_price", table_name="ticket_types")
    op.drop_index("ix_events_discovery_category", table_name="events")
    op.drop_index("ix_events_discovery_order", table_name="events")
