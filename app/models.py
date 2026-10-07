"""Initial domain schema. Authorization and lifecycle rules belong in services."""

from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Record:
    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()
    )


class DiscoveryVersion(Base):
    """Durable cache generation changed in the same transaction as organiser writes."""

    __tablename__ = "discovery_version"
    __table_args__ = (sa.CheckConstraint("id = 1", name="singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(sa.BigInteger, server_default="0")


class User(Record, Base):
    __tablename__ = "users"
    __table_args__ = (sa.CheckConstraint("email = lower(trim(email))", name="normalized_email"),)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    display_name: Mapped[str] = mapped_column(sa.String(120))
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_admin: Mapped[bool] = mapped_column(server_default=sa.false())


class Organizer(Record, Base):
    __tablename__ = "organizers"
    __table_args__ = (
        sa.CheckConstraint("status IN ('pending','approved','suspended')", name="status"),
    )
    name: Mapped[str] = mapped_column(sa.String(160))
    slug: Mapped[str] = mapped_column(sa.String(160), unique=True)
    status: Mapped[str] = mapped_column(sa.String(20), server_default="pending")


class Membership(Record, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        sa.UniqueConstraint("organizer_id", "user_id"),
        sa.CheckConstraint("role IN ('owner','editor','check_in')", name="role"),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizers.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(sa.String(20))


class Event(Record, Base):
    __tablename__ = "events"
    __table_args__ = (
        sa.UniqueConstraint("id", "organizer_id"),
        sa.Index("ix_events_discovery_order", "status", "starts_at", "id"),
        sa.Index("ix_events_discovery_category", "category", "status", "starts_at", "id"),
        sa.CheckConstraint("status IN ('draft','published','cancelled')", name="status"),
        sa.CheckConstraint("format IN ('in_person','online')", name="format"),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="timing"
        ),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizers.id"), index=True)
    slug: Mapped[str] = mapped_column(sa.String(180), unique=True)
    title: Mapped[str | None] = mapped_column(sa.String(200))
    description: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.String(20), server_default="draft", index=True)
    format: Mapped[str] = mapped_column(sa.String(20), server_default="in_person")
    timezone: Mapped[str] = mapped_column(sa.String(64), server_default="Asia/Kolkata")
    starts_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    venue: Mapped[str | None] = mapped_column(sa.String(300))
    city: Mapped[str | None] = mapped_column(sa.String(120), index=True)
    online_url: Mapped[str | None] = mapped_column(sa.Text)
    cover_url: Mapped[str | None] = mapped_column(sa.Text)
    category: Mapped[str | None] = mapped_column(sa.String(80), index=True)
    cancellation_reason: Mapped[str | None] = mapped_column(sa.String(1000))
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(sa.Integer, server_default="0")


sa.Index(
    "ix_events_discovery_city",
    sa.func.lower(Event.city),
    Event.status,
    Event.starts_at,
    Event.id,
).ddl_if(dialect="postgresql")
# PostgreSQL's canonical expression includes casts introduced by reflection.
# Keep metadata and queries identical so Alembic does not propose rebuilding
# the unchanged GIN index on every schema check.
EVENT_SEARCH_DOCUMENT = (
    "to_tsvector('simple'::regconfig, "
    "(((((COALESCE(title, ''::character varying)::text || ' '::text)"
    " || COALESCE(description, ''::text)) || ' '::text)"
    " || COALESCE(venue, ''::character varying)::text) || ' '::text)"
    " || COALESCE(city, ''::character varying)::text)"
)
sa.Index(
    "ix_events_discovery_search",
    sa.literal_column(EVENT_SEARCH_DOCUMENT),
    postgresql_using="gin",
    _table=Event.__table__,
).ddl_if(dialect="postgresql")


class TicketType(Record, Base):
    __tablename__ = "ticket_types"
    __table_args__ = (
        sa.Index("ix_ticket_types_discovery_price", "event_id", "capacity", "price_minor"),
        sa.ForeignKeyConstraint(["event_id", "organizer_id"], ["events.id", "events.organizer_id"]),
        sa.UniqueConstraint("id", "event_id", "organizer_id"),
        sa.CheckConstraint("price_minor >= 0", name="price"),
        sa.CheckConstraint("capacity >= 0", name="capacity"),
        sa.CheckConstraint("currency = 'INR'", name="currency"),
        sa.CheckConstraint("per_order_limit > 0", name="order_limit"),
        sa.CheckConstraint(
            "sales_start IS NULL OR sales_end IS NULL OR sales_end > sales_start",
            name="sales_window",
        ),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    price_minor: Mapped[int] = mapped_column(sa.BigInteger, server_default="0")
    currency: Mapped[str] = mapped_column(sa.String(3), server_default="INR")
    capacity: Mapped[int] = mapped_column(sa.Integer)
    sales_start: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sales_end: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    per_order_limit: Mapped[int] = mapped_column(sa.Integer, server_default="10")


class Booking(Record, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        sa.Index("ix_bookings_inventory", "event_id", "status", "expires_at"),
        sa.ForeignKeyConstraint(["event_id", "organizer_id"], ["events.id", "events.organizer_id"]),
        sa.UniqueConstraint("id", "event_id", "organizer_id"),
        sa.UniqueConstraint("user_id", "idempotency_key"),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','expired','cancelled')", name="status"
        ),
        sa.CheckConstraint("total_minor >= 0", name="total"),
        sa.CheckConstraint("currency = 'INR'", name="currency"),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128))
    status: Mapped[str] = mapped_column(sa.String(20), server_default="pending")
    total_minor: Mapped[int] = mapped_column(sa.BigInteger)
    currency: Mapped[str] = mapped_column(sa.String(3), server_default="INR")
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), index=True)


class Ticket(Record, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["booking_id", "event_id", "organizer_id"],
            ["bookings.id", "bookings.event_id", "bookings.organizer_id"],
        ),
        sa.ForeignKeyConstraint(
            ["ticket_type_id", "event_id", "organizer_id"],
            ["ticket_types.id", "ticket_types.event_id", "ticket_types.organizer_id"],
        ),
        sa.UniqueConstraint("booking_id", "sequence"),
        sa.UniqueConstraint("id", "event_id", "organizer_id"),
        sa.CheckConstraint("sequence > 0", name="sequence"),
        sa.CheckConstraint("price_minor >= 0", name="price"),
        sa.CheckConstraint("status IN ('valid','cancelled')", name="status"),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    booking_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    ticket_type_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    sequence: Mapped[int] = mapped_column(sa.Integer)
    price_minor: Mapped[int] = mapped_column(sa.BigInteger)
    qr_token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    status: Mapped[str] = mapped_column(sa.String(20), server_default="valid")
    admitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    admitted_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)


class AuditEntry(Record, Base):
    __tablename__ = "audit_entries"
    organizer_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizers.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(sa.String(100))
    target_id: Mapped[UUID] = mapped_column(sa.Uuid)
    reason: Mapped[str | None] = mapped_column(sa.String(1000))


class AccountToken(Record, Base):
    __tablename__ = "account_tokens"
    __table_args__ = (sa.CheckConstraint("purpose IN ('verify','recover')", name="purpose"),)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(sa.String(20))
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class Outbox(Record, Base):
    __tablename__ = "notification_outbox"
    kind: Mapped[str] = mapped_column(sa.String(30))
    payload: Mapped[dict] = mapped_column(sa.JSON)
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    organizer_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("organizers.id"), index=True)
    booking_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("bookings.id"))
    topic: Mapped[str | None] = mapped_column(sa.String(40))
    dedupe_key: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    failed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    skipped_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(sa.String(40))


class EventMedia(Record, Base):
    __tablename__ = "event_media"
    __table_args__ = (
        sa.Index(
            "ix_event_media_discovery_poster", "event_id", "kind", "status", "created_at", "id"
        ),
        sa.ForeignKeyConstraint(["event_id", "organizer_id"], ["events.id", "events.organizer_id"]),
        sa.CheckConstraint("kind IN ('poster','gallery')", name="kind"),
        sa.CheckConstraint("status IN ('processing','ready','failed')", name="status"),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    kind: Mapped[str] = mapped_column(sa.String(20))
    status: Mapped[str] = mapped_column(sa.String(20), server_default="processing")
    source_key: Mapped[str] = mapped_column(sa.String(300), unique=True)
    image_key: Mapped[str | None] = mapped_column(sa.String(300))
    thumbnail_key: Mapped[str | None] = mapped_column(sa.String(300))
    alt_text: Mapped[str] = mapped_column(sa.String(300))
    error: Mapped[str | None] = mapped_column(sa.String(300))


class BookingItem(Record, Base):
    __tablename__ = "booking_items"
    __table_args__ = (
        sa.Index("ix_booking_items_inventory", "ticket_type_id", "booking_id"),
        sa.ForeignKeyConstraint(
            ["booking_id", "event_id", "organizer_id"],
            ["bookings.id", "bookings.event_id", "bookings.organizer_id"],
        ),
        sa.ForeignKeyConstraint(
            ["ticket_type_id", "event_id", "organizer_id"],
            ["ticket_types.id", "ticket_types.event_id", "ticket_types.organizer_id"],
        ),
        sa.UniqueConstraint("booking_id", "ticket_type_id"),
        sa.CheckConstraint("quantity > 0 AND unit_price_minor >= 0", name="amounts"),
    )
    booking_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid)
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid)
    ticket_type_id: Mapped[UUID] = mapped_column(sa.Uuid)
    quantity: Mapped[int] = mapped_column(sa.Integer)
    unit_price_minor: Mapped[int] = mapped_column(sa.BigInteger)


class PaymentAttempt(Record, Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["booking_id", "event_id", "organizer_id"],
            ["bookings.id", "bookings.event_id", "bookings.organizer_id"],
        ),
        sa.CheckConstraint("amount_minor >= 0 AND currency = 'INR'", name="amount"),
        sa.CheckConstraint("status IN ('pending','paid','failed','refunded')", name="status"),
    )
    booking_id: Mapped[UUID] = mapped_column(sa.Uuid, index=True)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid)
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid)
    next_check_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), index=True
    )
    provider: Mapped[str] = mapped_column(sa.String(80))
    provider_reference: Mapped[str | None] = mapped_column(sa.String(200), unique=True)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), unique=True)
    status: Mapped[str] = mapped_column(sa.String(20), server_default="pending")
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger)
    currency: Mapped[str] = mapped_column(sa.String(3), server_default="INR")


class WebhookReceipt(Record, Base):
    """Authenticated notification metadata only; never retain provider payloads."""

    __tablename__ = "webhook_receipts"
    digest: Mapped[str] = mapped_column(sa.String(64), unique=True)
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Refund(Record, Base):
    """One full sandbox refund intent per collected payment, persisted before I/O."""

    __tablename__ = "refunds"
    __table_args__ = (
        sa.CheckConstraint("amount_minor > 0 AND currency = 'INR'", name="amount"),
        sa.CheckConstraint("status IN ('queued','pending','succeeded','failed')", name="status"),
        sa.CheckConstraint(
            "reason IN ('event_cancelled','reservation_unavailable')", name="reason"
        ),
    )
    payment_attempt_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("payment_attempts.id"), unique=True
    )
    provider_reference: Mapped[str] = mapped_column(sa.String(40), unique=True)
    amount_minor: Mapped[int] = mapped_column(sa.BigInteger)
    currency: Mapped[str] = mapped_column(sa.String(3), server_default="INR")
    reason: Mapped[str] = mapped_column(sa.String(32))
    status: Mapped[str] = mapped_column(sa.String(20), server_default="queued")
    provider_status: Mapped[str | None] = mapped_column(sa.String(32))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class CheckIn(Record, Base):
    __tablename__ = "check_ins"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["ticket_id", "event_id", "organizer_id"],
            ["tickets.id", "tickets.event_id", "tickets.organizer_id"],
        ),
        sa.UniqueConstraint("ticket_id"),
    )
    ticket_id: Mapped[UUID] = mapped_column(sa.Uuid)
    event_id: Mapped[UUID] = mapped_column(sa.Uuid)
    organizer_id: Mapped[UUID] = mapped_column(sa.Uuid)
    actor_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"))


class EventReport(Record, Base):
    __tablename__ = "event_reports"
    __table_args__ = (
        sa.CheckConstraint("status IN ('open','resolved','dismissed')", name="status"),
    )
    organizer_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizers.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(sa.ForeignKey("events.id"), index=True)
    reporter_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(sa.String(1000))
    status: Mapped[str] = mapped_column(sa.String(20), server_default="open")
    resolution: Mapped[str | None] = mapped_column(sa.String(1000))
    resolved_by: Mapped[UUID | None] = mapped_column(sa.ForeignKey("users.id"))


class SavedEvent(Record, Base):
    __tablename__ = "saved_events"
    __table_args__ = (sa.UniqueConstraint("user_id", "event_id"),)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(sa.ForeignKey("events.id"), index=True)


class OrganizerFollow(Record, Base):
    __tablename__ = "organizer_follows"
    __table_args__ = (sa.UniqueConstraint("user_id", "organizer_id"),)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    organizer_id: Mapped[UUID] = mapped_column(sa.ForeignKey("organizers.id"), index=True)


class WaitlistEntry(Record, Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (
        sa.Index("ix_waitlist_entries_due", "status", "next_check_at", "id"),
        sa.UniqueConstraint("user_id", "ticket_type_id"),
        sa.CheckConstraint("status IN ('waiting','notified')", name="status"),
    )
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    ticket_type_id: Mapped[UUID] = mapped_column(sa.ForeignKey("ticket_types.id"), index=True)
    status: Mapped[str] = mapped_column(sa.String(20), server_default="waiting")
    next_check_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), index=True
    )
