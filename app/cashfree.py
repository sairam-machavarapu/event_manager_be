"""Cashfree sandbox only. Production collection requires marketplace onboarding."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import DB
from app.bookings import (
    bookable,
    effective_status,
    issue_tickets,
    lock_event,
    lock_user,
    view,
)
from app.config import get_settings
from app.discovery import utc
from app.models import Booking, BookingItem, PaymentAttempt, User, WebhookReceipt
from app.payments import prepare_attempt
from app.workspaces import MUTATIONS, Actor

router = APIRouter(prefix="/api/v1", tags=["payments"])


def configured():
    settings = get_settings()
    if (
        not settings.cashfree_enabled
        or not settings.cashfree_client_id
        or not settings.cashfree_client_secret
    ):
        raise HTTPException(503, "Sandbox payments are not configured yet")
    return settings


async def provider_request(
    method, path, *, body=None, key=None, missing_ok=False, allow_list=False
):
    settings = configured()
    headers = {
        "x-client-id": settings.cashfree_client_id,
        "x-client-secret": settings.cashfree_client_secret,
        "x-api-version": settings.cashfree_api_version,
    }
    if key:
        headers["x-idempotency-key"] = key
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method, "https://sandbox.cashfree.com/pg" + path, headers=headers, json=body
            )
        if missing_ok and response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) and not (allow_list and isinstance(data, list)):
            raise ValueError("Invalid provider response")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        # Never expose provider response bodies or credentials to the browser.
        raise HTTPException(
            502, "Payment provider unavailable. Check status before retrying."
        ) from exc


def matches(order, attempt):
    try:
        amount = Decimal(str(order.get("order_amount"))) * 100
        return (
            amount.is_finite()
            and amount == attempt.amount_minor
            and order.get("order_currency") == attempt.currency
            and order.get("order_id") == attempt.provider_reference
        )
    except InvalidOperation, TypeError, ValueError:
        return False


class PaymentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(pattern=r"^[6-9][0-9]{9}$")


@router.get("/payments/config")
async def payment_config():
    settings = get_settings()
    return {
        "enabled": bool(
            settings.cashfree_enabled
            and settings.cashfree_client_id
            and settings.cashfree_client_secret
        ),
        "mode": "sandbox",
    }


@router.post("/bookings/{booking_id}/payment", dependencies=MUTATIONS)
async def start_payment(booking_id: UUID, body: PaymentInput, db: DB, user: Actor):
    settings = configured()
    attempt = await prepare_attempt(db, user, booking_id, "cashfree")
    if attempt.amount_minor < 100:
        raise HTTPException(409, "Cashfree payments require a total of at least ₹1")
    attempt.provider_reference = f"gather_{booking_id.hex}"
    booking = await db.scalar(sa.select(Booking).where(Booking.id == booking_id))
    order_id, key = attempt.provider_reference, str(attempt.id)
    payload = {
        "order_id": order_id,
        "order_amount": float(Decimal(attempt.amount_minor) / 100),
        "order_currency": attempt.currency,
        "order_expiry_time": utc(booking.expires_at).isoformat(),
        "customer_details": {
            "customer_id": user.id.hex,
            "customer_email": user.email,
            "customer_phone": body.phone,
        },
        "order_meta": {"return_url": f"{settings.auth_origin}/checkout/{booking_id}"},
    }
    if settings.cashfree_notify_url:
        payload["order_meta"]["notify_url"] = settings.cashfree_notify_url
    await db.commit()  # Stable intent survives ambiguous provider/network failures.
    order = await provider_request("GET", f"/orders/{order_id}", missing_ok=True)
    if order is None:
        order = await provider_request("POST", "/orders", body=payload, key=key)
    if not matches(order, attempt):
        raise HTTPException(502, "Payment provider returned an inconsistent order")
    if order.get("order_status") != "ACTIVE":
        raise HTTPException(409, "Check payment status before continuing")
    session = order.get("payment_session_id")
    if not isinstance(session, str) or not session:
        raise HTTPException(502, "Payment session unavailable. Check status before retrying.")
    return {"payment_session_id": session, "mode": "sandbox"}


async def reconcile(db, attempt_id, order):
    # Same user -> workspace -> event -> booking lock order as reservations.
    attempt = await db.scalar(sa.select(PaymentAttempt).where(PaymentAttempt.id == attempt_id))
    booking = await db.scalar(sa.select(Booking).where(Booking.id == attempt.booking_id))
    actor = await db.scalar(sa.select(User).where(User.id == booking.user_id))
    actor = await lock_user(db, actor)
    event, host = await lock_event(db, booking.event_id)
    booking = await db.scalar(
        sa.select(Booking)
        .where(Booking.id == booking.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        sa.select(PaymentAttempt)
        .where(PaymentAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not matches(order, attempt):
        raise HTTPException(409, "Payment amount or order does not match the reservation")
    now = datetime.now(UTC)
    if order.get("order_status") == "PAID" and attempt.status == "pending":
        attempt.status = "paid"
        try:
            bookable(event, host, now)
            eligible = effective_status(booking, now) == "pending"
        except HTTPException:
            eligible = False
        if eligible:
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
            if (
                not items
                or sum(item.quantity * item.unit_price_minor for item in items)
                != attempt.amount_minor
            ):
                raise HTTPException(409, "Reservation amount is inconsistent")
            await issue_tickets(db, booking, event, actor, items)
        # Late/released/cancelled payments are recorded for refund review;
        # never resurrect a hold or issue tickets against released inventory.
    if attempt.status == "paid" and booking.status != "confirmed":
        from app.refunds import queue_refund

        await queue_refund(db, attempt, "reservation_unavailable")
    await db.flush()
    result = await view(db, booking, now)
    await db.commit()
    return result


@router.post("/bookings/{booking_id}/payment-status", dependencies=MUTATIONS)
async def payment_status(booking_id: UUID, db: DB, user: Actor):
    booking = await db.scalar(
        sa.select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    )
    if booking is None:
        raise HTTPException(404, "Reservation not found")
    attempt = await db.scalar(
        sa.select(PaymentAttempt).where(
            PaymentAttempt.booking_id == booking_id, PaymentAttempt.provider == "cashfree"
        )
    )
    if attempt is None or not attempt.provider_reference:
        return await view(db, booking, datetime.now(UTC))
    if attempt.status in {"paid", "refunded"}:
        return await reconcile(
            db,
            attempt.id,
            {
                "order_id": attempt.provider_reference,
                "order_amount": str(Decimal(attempt.amount_minor) / 100),
                "order_currency": attempt.currency,
                "order_status": "PAID",
            },
        )
    order = await provider_request("GET", f"/orders/{attempt.provider_reference}", missing_ok=True)
    if order is None:
        return await view(db, booking, datetime.now(UTC))
    return await reconcile(db, attempt.id, order)


@router.post("/payments/cashfree/webhook")
async def webhook(request: Request, db: DB):
    settings = configured()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 65536:
            raise HTTPException(413, "Webhook too large")
        raw.extend(chunk)
    raw = bytes(raw)
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")
    expected = base64.b64encode(
        hmac.new(
            settings.cashfree_client_secret.encode(), timestamp.encode() + raw, hashlib.sha256
        ).digest()
    ).decode()
    if not timestamp or not signature.isascii() or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid payment signature")
    digest = hashlib.sha256(raw).hexdigest()
    # Insert once using the database's conflict handling, including concurrent deliveries.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    await db.execute(insert(WebhookReceipt).values(digest=digest).on_conflict_do_nothing())
    await db.commit()  # Receipt survives provider timeouts and transaction rollbacks.
    receipt = await db.scalar(sa.select(WebhookReceipt).where(WebhookReceipt.digest == digest))
    if receipt.processed_at is not None:
        return {"received": True}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid webhook")
        if str(payload.get("type", "")).startswith("REFUND"):
            from app.models import Refund
            from app.refunds import RefundPending, process_refund

            refund_reference = payload["data"]["refund"]["refund_id"]
            if not isinstance(refund_reference, str):
                raise ValueError("Invalid refund reference")
            row = await db.scalar(
                sa.select(Refund).where(Refund.provider_reference == refund_reference)
            )
            if row is None:
                raise HTTPException(404, "Refund not found")
            try:
                await process_refund(db, row.id, query_failed=True)
            except RefundPending:
                pass
            receipt.processed_at = datetime.now(UTC)
            await db.commit()
            return {"received": True}
        order_id = payload["data"]["order"]["order_id"]
        if not isinstance(order_id, str):
            raise ValueError("Invalid order")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "Invalid payment webhook") from exc
    attempt = await db.scalar(
        sa.select(PaymentAttempt).where(
            PaymentAttempt.provider_reference == order_id, PaymentAttempt.provider == "cashfree"
        )
    )
    if attempt is None:
        raise HTTPException(404, "Payment order not found")
    # Signed notification triggers authoritative verification, never direct fulfilment.
    if attempt.status != "paid":
        order = await provider_request("GET", f"/orders/{order_id}")
        await reconcile(db, attempt.id, order)
    if attempt.status in {"paid", "refunded"}:
        receipt.processed_at = datetime.now(UTC)
    await db.commit()
    return {"received": True}
