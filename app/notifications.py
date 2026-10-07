"""Transactional mail templates, durable reminders and private owner operations."""

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException

from app.auth import DB
from app.config import get_settings
from app.discovery import utc
from app.models import Booking, Event, Organizer, Outbox, User
from app.workspaces import MUTATIONS, Actor, access, audit

router = APIRouter(prefix="/api/v1/workspaces/{organizer_id}/notifications", tags=["notifications"])


def email_payload(to, subject, text, *, link=None, label="View registration"):
    html = '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#18372c">'
    html += f'<p style="font-weight:bold">Gather</p><h1>{escape(subject)}</h1>'
    html += "".join(f"<p>{escape(line)}</p>" for line in text.split("\n") if line)
    if link:
        html += f'<p><a href="{escape(link, quote=True)}">{escape(label)}</a></p>'
        text += f"\n{label}: {link}"
    return {"to": to, "subject": subject, "text": text, "html": html + "</body></html>"}


def event_time(event):
    if not event.starts_at:
        return "Time to be confirmed"
    local = utc(event.starts_at).astimezone(ZoneInfo(event.timezone))
    return f"{local:%d %b %Y, %I:%M %p} ({event.timezone})"


def registration_email(user, event, booking, count):
    location = (
        "Online event"
        if event.format == "online"
        else ", ".join(filter(None, [event.venue, event.city]))
    )
    return email_payload(
        user.email,
        "Registration confirmed",
        f"Your registration for {event.title} is confirmed for {count} ticket(s).\n"
        f"Starts: {event_time(event)}\nLocation: {location}\n"
        f"Reservation: {booking.id}\nYour entry QR is available in My tickets.",
        link=f"{get_settings().auth_origin}/orders/{booking.id}/confirmation",
    )


async def schedule_reminders(db, booking, event, now=None):
    """Called under the workspace/event lock; dedupe survives clearing payloads."""
    now = now or datetime.now(UTC)
    if booking.status != "confirmed" or event.status != "published" or not event.starts_at:
        return
    start = utc(event.starts_at)
    if start <= now:
        return
    for hours in (24, 1):
        # Close-to-start bookings need only the imminent reminder, not two at once.
        if hours == 24 and start <= now + timedelta(hours=1):
            continue
        key = f"reminder:{booking.id}:{start.isoformat()}:{hours}"
        if await db.scalar(sa.select(Outbox.id).where(Outbox.dedupe_key == key)):
            continue
        db.add(
            Outbox(
                kind="reminder",
                organizer_id=booking.organizer_id,
                booking_id=booking.id,
                topic=f"reminder_{hours}h",
                dedupe_key=key,
                available_at=max(now, start - timedelta(hours=hours)),
                payload={"starts_at": start.isoformat(), "hours": hours},
            )
        )
        await db.flush()


async def recover_reminders(factory):
    """Backfill older confirmed bookings in bounded batches, without offset starvation."""
    from app.bookings import lock_event

    now = datetime.now(UTC)
    async with factory() as db:
        ids = list(
            (
                await db.execute(
                    sa.select(Booking.id)
                    .join(Event, Event.id == Booking.event_id)
                    .join(Organizer, Organizer.id == Booking.organizer_id)
                    .where(
                        Booking.status == "confirmed",
                        Event.status == "published",
                        Event.starts_at > now,
                        Organizer.status == "approved",
                        ~sa.exists().where(
                            Outbox.booking_id == Booking.id, Outbox.kind == "reminder"
                        ),
                    )
                    .order_by(Event.starts_at, Booking.id)
                    .limit(100)
                )
            ).scalars()
        )
    for booking_id in ids:
        async with factory() as db, db.begin():
            booking = await db.scalar(sa.select(Booking).where(Booking.id == booking_id))
            if booking is None:
                continue
            event, host = await lock_event(db, booking.event_id)
            booking = await db.scalar(
                sa.select(Booking)
                .where(Booking.id == booking_id)
                .execution_options(populate_existing=True)
            )
            if host.status == "approved":
                await schedule_reminders(db, booking, event)


async def reminder_payload(db, message):
    """Recheck eligibility immediately before SMTP while holding event/workspace locks."""
    from app.bookings import lock_event

    booking = await db.scalar(sa.select(Booking).where(Booking.id == message.booking_id))
    if booking is None:
        return None
    event, host = await lock_event(db, booking.event_id)
    booking = await db.scalar(
        sa.select(Booking)
        .where(Booking.id == message.booking_id)
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    scheduled_start = datetime.fromisoformat(message.payload["starts_at"])
    hours = message.payload["hours"]
    if (
        booking.status != "confirmed"
        or event.status != "published"
        or host.status != "approved"
        or not event.starts_at
        or utc(event.starts_at) <= now
        or utc(event.starts_at) != scheduled_start
        or (hours == 24 and utc(event.starts_at) <= now + timedelta(hours=1))
    ):
        return None
    user = await db.scalar(sa.select(User).where(User.id == booking.user_id))
    if user.verified_at is None:
        return None
    location = (
        "Online event"
        if event.format == "online"
        else ", ".join(filter(None, [event.venue, event.city]))
    )
    return email_payload(
        user.email,
        "Your Gather event is coming up",
        f"You are registered for {event.title}.\nStarts: {event_time(event)}\n"
        f"Location: {location}\nReservation: {booking.id}\n"
        "Your entry QR is available in My tickets.",
        link=f"{get_settings().auth_origin}/orders/{booking.id}/confirmation",
    )


def delivery_view(message):
    state = (
        "delivered"
        if message.delivered_at
        else "skipped"
        if message.skipped_at
        else "failed"
        if message.failed_at
        else "retrying"
        if message.attempts
        else "scheduled"
    )
    return {
        "id": message.id,
        "booking_id": message.booking_id,
        "topic": message.topic,
        "status": state,
        "attempts": message.attempts,
        "available_at": message.available_at,
        "delivered_at": message.delivered_at,
        "last_error_code": message.last_error_code,
    }


@router.get("")
async def list_notifications(organizer_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles={"owner"})
    rows = (
        await db.execute(
            sa.select(Outbox)
            .where(
                Outbox.organizer_id == organizer_id,
                Outbox.kind.in_(["email", "reminder", "engagement"]),
            )
            .order_by(Outbox.created_at.desc(), Outbox.id)
            .limit(100)
        )
    ).scalars()
    return [delivery_view(row) for row in rows]


@router.post("/{message_id}/retry", dependencies=MUTATIONS)
async def retry_notification(organizer_id: UUID, message_id: UUID, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    message = await db.scalar(
        sa.select(Outbox)
        .where(
            Outbox.id == message_id,
            Outbox.organizer_id == organizer_id,
            Outbox.kind.in_(["email", "reminder", "engagement"]),
        )
        .with_for_update()
    )
    if message is None:
        raise HTTPException(404, "Notification not found")
    if not message.failed_at or message.delivered_at or message.skipped_at:
        raise HTTPException(409, "Only failed notifications can be retried")
    message.failed_at, message.last_error_code = None, None
    message.attempts = 0
    message.available_at = datetime.now(UTC)
    audit(db, user, workspace, "notification.retry", message.id)
    await db.commit()
    return delivery_view(message)
