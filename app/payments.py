"""Provider-independent payment preparation. No collection or confirmation yet."""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException

from app.bookings import bookable, effective_status, lock_event, lock_user
from app.models import Booking, BookingItem, PaymentAttempt


async def prepare_attempt(db, user, booking_id: UUID, provider: str) -> PaymentAttempt:
    """Persist one stable attempt per reservation/provider before provider I/O.

    Caller commits before sending any provider request. Retrying always uses the
    same key and snapshotted amount, including after an uncertain network result.
    This helper deliberately cannot mark a payment paid or issue tickets.
    """
    if provider not in {"cashfree", "razorpay"}:
        raise ValueError("Unsupported payment provider")
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
    if actor.verified_at is None:
        raise HTTPException(403, "Verify your email before paying")
    if effective_status(booking, now) != "pending":
        raise HTTPException(409, "This reservation is no longer active")
    bookable(event, host, now)
    if booking.total_minor <= 0 or booking.currency != "INR":
        raise HTTPException(409, "Payment requires a paid INR reservation")
    items = (
        (await db.execute(sa.select(BookingItem).where(BookingItem.booking_id == booking.id)))
        .scalars()
        .all()
    )
    if (
        not items
        or sum(item.quantity * item.unit_price_minor for item in items) != booking.total_minor
    ):
        raise HTTPException(409, "Reservation amount is inconsistent")
    key = f"payment_{booking.id.hex}"
    previous = await db.scalar(
        sa.select(PaymentAttempt).where(PaymentAttempt.idempotency_key == key)
    )
    if previous:
        if (
            previous.provider != provider
            or previous.amount_minor != booking.total_minor
            or previous.currency != booking.currency
            or previous.status != "pending"
        ):
            raise HTTPException(409, "Existing payment attempt requires reconciliation")
        return previous
    attempt = PaymentAttempt(
        booking_id=booking.id,
        event_id=booking.event_id,
        organizer_id=booking.organizer_id,
        provider=provider,
        idempotency_key=key,
        amount_minor=booking.total_minor,
        currency=booking.currency,
        status="pending",
    )
    db.add(attempt)
    await db.flush()
    return attempt
