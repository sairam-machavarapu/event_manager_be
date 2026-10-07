"""Cancellation/late-payment refunds. Provider acceptance is not completion."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException

from app.auth import DB
from app.bookings import lock_event, view
from app.config import get_settings
from app.models import Booking, Outbox, PaymentAttempt, Refund, User
from app.notifications import email_payload
from app.workspaces import MUTATIONS, Actor, access

router = APIRouter(prefix="/api/v1", tags=["refunds"])


class RefundPending(Exception):
    """Keep the outbox message available for later reconciliation."""


async def queue_refund(db, attempt, reason):
    """Caller holds the workspace/event lock; inserts intent and job atomically."""
    existing = await db.scalar(sa.select(Refund).where(Refund.payment_attempt_id == attempt.id))
    if existing:
        return existing
    if attempt.status != "paid" or attempt.provider != "cashfree" or not attempt.provider_reference:
        raise HTTPException(
            409, "Collected payment requires manual reconciliation before refunding"
        )
    refund_id = uuid4()
    refund = Refund(
        id=refund_id,
        payment_attempt_id=attempt.id,
        provider_reference=f"refund{refund_id.hex}",
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        reason=reason,
        status="queued",
    )
    db.add(refund)
    db.add(Outbox(kind="refund", payload={"refund_id": str(refund_id)}))
    await db.flush()
    return refund


async def recover_refunds(factory):
    """Recover pre-Day-16 late payments or missed intents; one transaction per lock."""
    async with factory() as db:
        ids = list(
            (
                await db.execute(
                    sa.select(PaymentAttempt.id)
                    .join(Booking, Booking.id == PaymentAttempt.booking_id)
                    .outerjoin(Refund, Refund.payment_attempt_id == PaymentAttempt.id)
                    .where(
                        PaymentAttempt.status == "paid",
                        PaymentAttempt.provider == "cashfree",
                        Booking.status != "confirmed",
                        Refund.id.is_(None),
                    )
                    .order_by(PaymentAttempt.id)
                    .limit(100)
                )
            ).scalars()
        )
    for attempt_id in ids:
        async with factory() as db, db.begin():
            attempt = await db.scalar(
                sa.select(PaymentAttempt).where(PaymentAttempt.id == attempt_id)
            )
            await lock_event(db, attempt.event_id)
            attempt = await db.scalar(
                sa.select(PaymentAttempt)
                .where(PaymentAttempt.id == attempt_id)
                .execution_options(populate_existing=True)
            )
            booking = await db.scalar(
                sa.select(Booking)
                .where(Booking.id == attempt.booking_id)
                .execution_options(populate_existing=True)
            )
            if attempt.status == "paid" and booking.status != "confirmed":
                await queue_refund(db, attempt, "reservation_unavailable")


def refund_matches(data, refund, attempt):
    try:
        amount = Decimal(str(data.get("refund_amount"))) * 100
        return (
            amount.is_finite()
            and amount == refund.amount_minor == attempt.amount_minor
            and data.get("refund_currency") == refund.currency == attempt.currency
            and data.get("order_id") == attempt.provider_reference
            and data.get("refund_id") == refund.provider_reference
        )
    except InvalidOperation, TypeError, ValueError:
        return False


async def process_refund(db, refund_id, *, query_failed=False):
    from app.cashfree import provider_request

    row = await db.scalar(sa.select(Refund).where(Refund.id == refund_id))
    if row is None:
        return
    attempt = await db.scalar(
        sa.select(PaymentAttempt).where(PaymentAttempt.id == row.payment_attempt_id)
    )
    # Cancellation/payment reconciliation acquire this same workspace lock first.
    await lock_event(db, attempt.event_id)
    attempt = await db.scalar(
        sa.select(PaymentAttempt)
        .where(PaymentAttempt.id == attempt.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await db.scalar(
        sa.select(Refund)
        .where(Refund.id == refund_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.status == "succeeded" or (row.status == "failed" and not query_failed):
        return
    if attempt.status not in {"paid", "refunded"} or attempt.provider != "cashfree":
        raise HTTPException(409, "Refund has no verified sandbox collection")
    path = f"/orders/{attempt.provider_reference}/refunds"
    data = await provider_request("GET", f"{path}/{row.provider_reference}", missing_ok=True)
    if data is None:
        if row.status != "queued":
            raise HTTPException(
                502, "Existing refund could not be reconciled; no new refund was sent"
            )
        # The committed refund UUID also serves as the provider idempotency key.
        await provider_request(
            "POST",
            path,
            key=str(row.id),
            allow_list=True,
            body={
                "refund_id": row.provider_reference,
                "refund_amount": float(Decimal(row.amount_minor) / 100),
                "refund_note": row.reason,
                "refund_speed": "STANDARD",
            },
        )
        row.status = "pending"
        # Even a successful create response requires an authoritative read.
        data = await provider_request("GET", f"{path}/{row.provider_reference}")
    if not refund_matches(data, row, attempt):
        raise HTTPException(502, "Refund amount or reference did not match")
    provider_status = data.get("refund_status")
    if provider_status not in {
        "SUCCESS",
        "PENDING",
        "PENDING_APPROVAL",
        "ONHOLD",
        "CANCELLED",
        "REJECTED",
        "FAILED",
    }:
        raise HTTPException(502, "Unknown refund status; reconciliation will retry")
    row.provider_status = provider_status
    if provider_status == "SUCCESS":
        row.status = "succeeded"
        row.completed_at = datetime.now(UTC)
        attempt.status = "refunded"
        booking = await db.scalar(sa.select(Booking).where(Booking.id == attempt.booking_id))
        email = await db.scalar(sa.select(User.email).where(User.id == booking.user_id))
        db.add(
            Outbox(
                kind="email",
                organizer_id=booking.organizer_id,
                booking_id=booking.id,
                topic="refund",
                payload=email_payload(
                    email,
                    "Refund confirmed",
                    f"Your sandbox refund of INR {Decimal(row.amount_minor) / 100:.2f} "
                    f"for reservation {booking.id} is confirmed. "
                    f"Reference: {row.provider_reference}.",
                    link=f"{get_settings().auth_origin}/orders/{booking.id}/confirmation",
                ),
            )
        )
    elif provider_status in {"CANCELLED", "REJECTED", "FAILED"}:
        row.status = "failed"
    else:
        row.status = "pending"
        raise RefundPending()


@router.post("/bookings/{booking_id}/refund-status", dependencies=MUTATIONS)
async def refund_status(booking_id: UUID, db: DB, user: Actor):
    booking = await db.scalar(
        sa.select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    if booking is None:
        raise HTTPException(404, "Reservation not found")
    refund = await db.scalar(
        sa.select(Refund).join(PaymentAttempt).where(PaymentAttempt.booking_id == booking_id)
    )
    if refund:
        try:
            await process_refund(db, refund.id, query_failed=True)
        except RefundPending:
            pass
        await db.flush()
        # Refresh changes made by cancellation while waiting for the workspace lock.
        booking = await db.scalar(
            sa.select(Booking)
            .where(Booking.id == booking_id)
            .execution_options(populate_existing=True)
        )
        result = await view(db, booking, datetime.now(UTC))
        await db.commit()
        return result
    return await view(db, booking, datetime.now(UTC))


@router.get("/workspaces/{organizer_id}/refunds")
async def workspace_refunds(organizer_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles={"owner"})
    rows = (
        await db.execute(
            sa.select(Refund, PaymentAttempt.booking_id)
            .join(PaymentAttempt)
            .where(PaymentAttempt.organizer_id == organizer_id)
            .order_by(Refund.created_at.desc(), Refund.id)
            .limit(100)
        )
    ).all()
    return [
        {
            "id": row.id,
            "booking_id": booking_id,
            "reference": row.provider_reference,
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "status": row.status,
            "provider_status": row.provider_status,
            "reason": row.reason,
            "completed_at": row.completed_at,
        }
        for row, booking_id in rows
    ]
