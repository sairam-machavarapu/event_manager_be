"""Private calendar downloads for confirmed registrations."""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Response

from app.auth import DB
from app.config import get_settings
from app.discovery import utc
from app.models import Booking, Event, Organizer
from app.workspaces import Actor

router = APIRouter(prefix="/api/v1/bookings", tags=["calendar"])


def escape(value):
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold(line):
    result, current, size = [], "", 0
    for char in line:
        length = len(char.encode("utf-8"))
        if size + length > 75:
            result.append(current)
            current, size = " ", 1
        current += char
        size += length
    result.append(current)
    return "\r\n".join(result)


@router.get("/{booking_id}/calendar")
async def download(booking_id: UUID, db: DB, user: Actor):
    row = (
        await db.execute(
            sa.select(Booking, Event, Organizer)
            .join(Event, Event.id == Booking.event_id)
            .join(Organizer, Organizer.id == Event.organizer_id)
            .where(Booking.id == booking_id, Booking.user_id == user.id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Registration not found")
    booking, event, host = row
    if (
        booking.status != "confirmed"
        or event.status != "published"
        or host.status != "approved"
        or not event.starts_at
        or not event.ends_at
    ):
        raise HTTPException(409, "Calendar download requires an available confirmed registration")

    def stamp(value):
        return utc(value).strftime("%Y%m%dT%H%M%SZ")

    location = (
        "Online event"
        if event.format == "online"
        else ", ".join(filter(None, [event.venue, event.city]))
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Gather//Event calendar//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{booking.id}@gather.local",
        f"DTSTAMP:{stamp(datetime.now(UTC))}",
        f"DTSTART:{stamp(event.starts_at)}",
        f"DTEND:{stamp(event.ends_at)}",
        f"SUMMARY:{escape(event.title or 'Gather event')}",
        f"LOCATION:{escape(location)}",
        f"URL:{get_settings().auth_origin}/events/{event.slug}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return Response(
        "\r\n".join(fold(line) for line in lines) + "\r\n",
        media_type="text/calendar; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="gather-{booking.id}.ics"',
        },
    )
