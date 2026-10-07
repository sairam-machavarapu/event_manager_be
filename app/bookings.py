"""Database-backed inventory holds and atomic free booking confirmation."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import DB, token_hash
from app.discovery import utc
from app.models import (
    Booking,
    BookingItem,
    Event,
    Organizer,
    Outbox,
    PaymentAttempt,
    Refund,
    Ticket,
    TicketType,
    User,
)
from app.workspaces import MUTATIONS, Actor

router = APIRouter(prefix="/api/v1/bookings", tags=["reservations"])
public_router = APIRouter(prefix="/api/v1/events", tags=["availability"])
HOLD_SECONDS = 600


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_type_id: UUID
    quantity: int = Field(strict=True, ge=1, le=100)


class ReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    items: list[ItemInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_types(self):
        if len({item.ticket_type_id for item in self.items}) != len(self.items):
            raise ValueError("Choose each ticket type once")
        if sum(item.quantity for item in self.items) > 100:
            raise ValueError("Reserve at most 100 tickets per order")
        return self


class ItemView(BaseModel):
    ticket_type_id: UUID
    name: str
    quantity: int
    unit_price_minor: int


class RefundView(BaseModel):
    reference: str
    status: Literal["queued", "pending", "succeeded", "failed"]
    amount_minor: int
    currency: str
    completed_at: datetime | None


class ReservationView(BaseModel):
    id: UUID
    event_id: UUID
    status: Literal["pending", "confirmed", "expired", "cancelled"]
    total_minor: int
    currency: str
    expires_at: datetime | None
    server_time: datetime
    items: list[ItemView]
    ticket_ids: list[UUID]
    event_title: str
    event_slug: str
    event_available: bool
    payment_review: bool = False
    refund: RefundView | None = None


def effective_status(booking, now):
    if booking.status == "pending" and booking.expires_at and utc(booking.expires_at) <= now:
        return "expired"
    return booking.status


async def view(db, booking, now):
    event, host = (
        await db.execute(
            sa.select(Event, Organizer)
            .join(Organizer, Organizer.id == Event.organizer_id)
            .where(Event.id == booking.event_id)
        )
    ).one()
    rows = (
        await db.execute(
            sa.select(BookingItem, TicketType.name)
            .join(TicketType, TicketType.id == BookingItem.ticket_type_id)
            .where(BookingItem.booking_id == booking.id)
            .order_by(BookingItem.ticket_type_id)
        )
    ).all()
    refund = await db.scalar(
        sa.select(Refund).join(PaymentAttempt).where(PaymentAttempt.booking_id == booking.id)
    )
    return ReservationView(
        refund=RefundView(
            reference=refund.provider_reference,
            status=refund.status,
            amount_minor=refund.amount_minor,
            currency=refund.currency,
            completed_at=utc(refund.completed_at) if refund.completed_at else None,
        )
        if refund
        else None,
        payment_review=booking.status != "confirmed"
        and bool(
            await db.scalar(
                sa.select(PaymentAttempt.id).where(
                    PaymentAttempt.booking_id == booking.id, PaymentAttempt.status == "paid"
                )
            )
        ),
        id=booking.id,
        event_title=event.title or "Event",
        event_slug=event.slug,
        event_available=host.status == "approved"
        and event.status == "published"
        and bool(event.starts_at and utc(event.starts_at) > now),
        event_id=booking.event_id,
        status=effective_status(booking, now),
        total_minor=booking.total_minor,
        currency=booking.currency,
        expires_at=utc(booking.expires_at) if booking.expires_at else None,
        server_time=now,
        items=[
            ItemView(
                ticket_type_id=item.ticket_type_id,
                name=name,
                quantity=item.quantity,
                unit_price_minor=item.unit_price_minor,
            )
            for item, name in rows
        ],
        ticket_ids=list(
            (
                await db.execute(
                    sa.select(Ticket.id)
                    .where(Ticket.booking_id == booking.id)
                    .order_by(Ticket.sequence)
                )
            ).scalars()
        ),
    )


async def used_inventory(db, event_id, now):
    rows = await db.execute(
        sa.select(BookingItem.ticket_type_id, sa.func.sum(BookingItem.quantity))
        .join(Booking, Booking.id == BookingItem.booking_id)
        .where(
            Booking.event_id == event_id,
            sa.or_(
                Booking.status == "confirmed",
                sa.and_(
                    Booking.status == "pending",
                    sa.or_(Booking.expires_at > now, Booking.expires_at.is_(None)),
                ),
            ),
        )
        .group_by(BookingItem.ticket_type_id)
    )
    return dict(rows.all())


async def lock_user(db, user):
    # NO KEY UPDATE serializes account mutations while allowing foreign-key
    # references from cancellation audit records; FOR UPDATE would deadlock
    # user -> workspace confirmation against workspace -> audit insertion.
    return await db.scalar(
        sa.select(User)
        .where(User.id == user.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


async def lock_event(db, event_id):
    organizer_id = await db.scalar(sa.select(Event.organizer_id).where(Event.id == event_id))
    if organizer_id is None:
        raise HTTPException(404, "Event not found")
    # Existing event cancellation/team mutations acquire this same workspace lock.
    host = await db.scalar(
        sa.select(Organizer)
        .where(Organizer.id == organizer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    event = await db.scalar(
        sa.select(Event)
        .where(Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return event, host


def bookable(event, host, now):
    if host.status != "approved" or event.status == "draft":
        raise HTTPException(404, "Event not found")
    if event.status != "published" or not event.starts_at or utc(event.starts_at) <= now:
        raise HTTPException(409, "This event is not accepting reservations")


@router.post("", response_model=ReservationView, status_code=201, dependencies=MUTATIONS)
async def reserve(body: ReservationInput, db: DB, user: Actor, response: Response):
    response.headers["Cache-Control"] = "no-store"
    actor = await lock_user(db, user)
    now = datetime.now(UTC)
    previous = await db.scalar(
        sa.select(Booking).where(
            Booking.user_id == user.id, Booking.idempotency_key == body.idempotency_key
        )
    )
    if previous:
        result = await view(db, previous, now)
        expected = {item.ticket_type_id: item.quantity for item in body.items}
        if previous.event_id != body.event_id or expected != {
            item.ticket_type_id: item.quantity for item in result.items
        }:
            raise HTTPException(409, "Idempotency key was already used for another selection")
        response.status_code = 200
        return result
    if actor.verified_at is None:
        raise HTTPException(403, "Verify your email before reserving tickets")
    event, host = await lock_event(db, body.event_id)
    now = datetime.now(UTC)  # Time spent waiting for locks does not extend a closed sales window.
    bookable(event, host, now)
    active = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Booking)
        .where(Booking.user_id == user.id, Booking.status == "pending", Booking.expires_at > now)
    )
    if active >= 5:
        raise HTTPException(409, "Release an existing reservation before creating another")
    requested = {item.ticket_type_id: item.quantity for item in body.items}
    types = (
        (
            await db.execute(
                sa.select(TicketType)
                .where(TicketType.event_id == event.id, TicketType.id.in_(requested))
                .order_by(TicketType.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if len(types) != len(requested):
        raise HTTPException(422, "Choose ticket types belonging to this event")
    used = await used_inventory(db, event.id, now)
    expires = min(now + timedelta(seconds=HOLD_SECONDS), utc(event.starts_at))
    total = 0
    for ticket in types:
        quantity = requested[ticket.id]
        if quantity > ticket.per_order_limit:
            raise HTTPException(422, f"{ticket.name}: exceeds the per-order limit")
        if ticket.sales_start and utc(ticket.sales_start) > now:
            raise HTTPException(409, f"{ticket.name}: sales have not started")
        if ticket.sales_end and utc(ticket.sales_end) <= now:
            raise HTTPException(409, f"{ticket.name}: sales have closed")
        if quantity > ticket.capacity - used.get(ticket.id, 0):
            raise HTTPException(409, f"{ticket.name}: not enough tickets available")
        if ticket.sales_end:
            expires = min(expires, utc(ticket.sales_end))
        total += quantity * ticket.price_minor
    booking = Booking(
        event_id=event.id,
        organizer_id=host.id,
        user_id=user.id,
        idempotency_key=body.idempotency_key,
        status="pending",
        total_minor=total,
        currency="INR",
        expires_at=expires,
    )
    db.add(booking)
    await db.flush()
    for ticket in types:
        db.add(
            BookingItem(
                booking_id=booking.id,
                event_id=event.id,
                organizer_id=host.id,
                ticket_type_id=ticket.id,
                quantity=requested[ticket.id],
                unit_price_minor=ticket.price_minor,
            )
        )
    await db.flush()
    result = await view(db, booking, now)
    await db.commit()
    return result


@router.get("/{booking_id}", response_model=ReservationView)
async def get_reservation(booking_id: UUID, db: DB, user: Actor):
    booking = await db.scalar(
        sa.select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    if booking is None:
        raise HTTPException(404, "Reservation not found")
    return await view(db, booking, datetime.now(UTC))


@router.post("/{booking_id}/release", response_model=ReservationView, dependencies=MUTATIONS)
async def release(booking_id: UUID, db: DB, user: Actor):
    await lock_user(db, user)
    booking = await db.scalar(
        sa.select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    if booking is None:
        raise HTTPException(404, "Reservation not found")
    await lock_event(db, booking.event_id)
    booking = await db.scalar(
        sa.select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if booking.status == "confirmed":
        raise HTTPException(409, "Confirmed bookings cannot be released as reservations")
    if booking.status == "pending":
        booking.status = "expired" if effective_status(booking, now) == "expired" else "cancelled"
    result = await view(db, booking, now)
    await db.commit()
    return result


@router.post("/{booking_id}/confirm-free", response_model=ReservationView, dependencies=MUTATIONS)
async def confirm_free(booking_id: UUID, db: DB, user: Actor):
    actor = await lock_user(db, user)
    booking = await db.scalar(
        sa.select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    if booking is None:
        raise HTTPException(404, "Reservation not found")
    event, host = await lock_event(db, booking.event_id)
    booking = await db.scalar(
        sa.select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if booking.total_minor != 0:
        raise HTTPException(409, "Paid bookings require verified payment confirmation")
    if booking.status == "confirmed":
        return await view(db, booking, now)
    if effective_status(booking, now) != "pending":
        raise HTTPException(409, "This reservation is no longer active")
    if actor.verified_at is None:
        raise HTTPException(403, "Verify your email before confirming tickets")
    bookable(event, host, now)
    items = (
        (
            await db.execute(
                sa.select(BookingItem)
                .where(BookingItem.booking_id == booking.id)
                .order_by(BookingItem.ticket_type_id)
            )
        )
        .scalars()
        .all()
    )
    if not items or any(item.unit_price_minor != 0 for item in items):
        raise HTTPException(409, "Reservation has no valid free ticket selection")
    await issue_tickets(db, booking, event, actor, items)
    await db.flush()
    result = await view(db, booking, now)
    await db.commit()
    return result


async def issue_tickets(db, booking, event, actor, items):
    sequence = 0
    for item in items:
        for _ in range(item.quantity):
            sequence += 1
            # Private random seed for the stable HMAC admission credential.
            # The ticket-owner QR endpoint derives it without storing raw tokens.
            db.add(
                Ticket(
                    booking_id=booking.id,
                    event_id=booking.event_id,
                    organizer_id=booking.organizer_id,
                    ticket_type_id=item.ticket_type_id,
                    sequence=sequence,
                    price_minor=item.unit_price_minor,
                    qr_token_hash=token_hash(secrets.token_urlsafe(32)),
                    status="valid",
                )
            )
    booking.status = "confirmed"
    from app.notifications import registration_email, schedule_reminders

    db.add(
        Outbox(
            kind="email",
            organizer_id=booking.organizer_id,
            booking_id=booking.id,
            topic="registration",
            payload=registration_email(actor, event, booking, sequence),
        )
    )
    await schedule_reminders(db, booking, event)


class AvailabilityItem(BaseModel):
    ticket_type_id: UUID
    remaining: int
    sales_status: Literal["open", "upcoming", "closed", "sold_out", "unavailable"]


class Availability(BaseModel):
    server_time: datetime
    items: list[AvailabilityItem]


@public_router.get("/{slug}/availability", response_model=Availability)
async def availability(slug: str, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    event = await db.scalar(
        sa.select(Event)
        .join(Organizer)
        .where(
            Event.slug == slug,
            Event.status.in_(["published", "cancelled"]),
            Organizer.status == "approved",
        )
    )
    if event is None:
        raise HTTPException(404, "Event not found")
    now = datetime.now(UTC)
    used = await used_inventory(db, event.id, now)
    types = (
        (
            await db.execute(
                sa.select(TicketType)
                .where(TicketType.event_id == event.id)
                .order_by(TicketType.price_minor, TicketType.id)
            )
        )
        .scalars()
        .all()
    )
    items = []
    for ticket in types:
        remaining = max(0, ticket.capacity - used.get(ticket.id, 0))
        if event.status != "published" or not event.starts_at or utc(event.starts_at) <= now:
            status = "unavailable"
        elif ticket.sales_end and utc(ticket.sales_end) <= now:
            status = "closed"
        elif ticket.sales_start and utc(ticket.sales_start) > now:
            status = "upcoming"
        elif remaining == 0:
            status = "sold_out"
        else:
            status = "open"
        items.append(
            AvailabilityItem(ticket_type_id=ticket.id, remaining=remaining, sales_status=status)
        )
    return Availability(server_time=now, items=items)


async def expire_reservations(db):
    """Cleanup only: inventory ignores elapsed holds even if the scheduler is down."""
    now = datetime.now(UTC)
    ids = list(
        (
            await db.execute(
                sa.select(Booking.id)
                .where(Booking.status == "pending", Booking.expires_at <= now)
                .order_by(Booking.expires_at, Booking.id)
                .limit(500)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    if not ids:
        return
    await db.execute(
        sa.update(Booking)
        .where(Booking.id.in_(ids), Booking.status == "pending", Booking.expires_at <= now)
        .values(status="expired")
    )
