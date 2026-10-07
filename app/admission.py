"""Private ticket credentials and workspace-scoped atomic admission."""

import csv
import hashlib
import hmac
import io
import re
from datetime import UTC, datetime
from uuid import UUID

import qrcode
import qrcode.image.svg
import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator

from app.auth import DB
from app.discovery import utc
from app.models import Booking, CheckIn, Event, Ticket, TicketType, User
from app.workspaces import MUTATIONS, Actor, access, audit

router = APIRouter(prefix="/api/v1", tags=["tickets and admission"])


def credential(ticket):
    # The existing random, private 256-bit QR hash is a per-ticket HMAC key.
    # Legacy placeholders become stable credentials without persisting raw QR tokens.
    digest = hmac.new(ticket.qr_token_hash.encode(), str(ticket.id).encode(), hashlib.sha256)
    return f"gather:{ticket.id}:{digest.hexdigest()}"


def ticket_view(ticket, event, name):
    return dict(
        id=str(ticket.id),
        booking_id=str(ticket.booking_id),
        event_id=str(event.id),
        event_title=event.title,
        event_slug=event.slug,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        timezone=event.timezone,
        venue=event.venue,
        city=event.city,
        ticket_type=name,
        sequence=ticket.sequence,
        status=ticket.status,
        event_status=event.status,
        admitted_at=ticket.admitted_at,
    )


@router.get("/tickets")
async def my_tickets(db: DB, user: Actor, offset: int = Query(0, ge=0)):
    rows = (
        await db.execute(
            sa.select(Ticket, Event, TicketType.name)
            .join(Booking, Booking.id == Ticket.booking_id)
            .join(Event, Event.id == Ticket.event_id)
            .join(TicketType, TicketType.id == Ticket.ticket_type_id)
            .where(Booking.user_id == user.id)
            .order_by(Event.starts_at.desc(), Ticket.id)
            .offset(offset)
            .limit(51)
        )
    ).all()
    return {
        "items": [ticket_view(*row) for row in rows[:50]],
        "next_offset": offset + 50 if len(rows) > 50 else None,
    }


@router.get("/tickets/{ticket_id}/qr")
async def ticket_qr(ticket_id: UUID, db: DB, user: Actor):
    row = (
        await db.execute(
            sa.select(Ticket, Booking, Event)
            .join(Booking, Booking.id == Ticket.booking_id)
            .join(Event, Event.id == Ticket.event_id)
            .where(Ticket.id == ticket_id, Booking.user_id == user.id)
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Ticket not found")
    ticket, booking, event = row
    if ticket.status != "valid" or booking.status != "confirmed" or event.status != "published":
        raise HTTPException(409, "This ticket is unavailable")
    if ticket.admitted_at or (event.ends_at and utc(event.ends_at) <= datetime.now(UTC)):
        raise HTTPException(409, "This ticket has been used or the event has ended")
    svg = qrcode.make(credential(ticket), image_factory=qrcode.image.svg.SvgPathImage)
    return Response(
        svg.to_string(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
    )


async def scoped_event(db, workspace_id, event_id, user, *, mutation=False, owner=False):
    workspace, _ = await access(
        db,
        workspace_id,
        user,
        mutation=mutation,
        roles={"owner"} if owner else {"owner", "check_in"},
    )
    query = sa.select(Event).where(Event.id == event_id, Event.organizer_id == workspace_id)
    if mutation:
        query = query.with_for_update()
    event = await db.scalar(query.execution_options(populate_existing=True))
    if event is None:
        raise HTTPException(404, "Event not found")
    return workspace, event


@router.get("/workspaces/{workspace_id}/operations")
async def operations(workspace_id: UUID, db: DB, user: Actor):
    await access(db, workspace_id, user, roles={"owner", "check_in"})
    rows = (
        (
            await db.execute(
                sa.select(Event)
                .where(Event.organizer_id == workspace_id)
                .order_by(Event.starts_at.desc(), Event.id)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [dict(id=str(event.id), title=event.title, status=event.status) for event in rows]


def attendee_query(event_id):
    return (
        sa.select(Ticket, User, TicketType.name, Booking.status)
        .join(Booking, Booking.id == Ticket.booking_id)
        .join(User, User.id == Booking.user_id)
        .join(TicketType, TicketType.id == Ticket.ticket_type_id)
        .where(Ticket.event_id == event_id)
        .order_by(Ticket.created_at, Ticket.id)
    )


@router.get("/workspaces/{workspace_id}/events/{event_id}/attendees")
async def attendees(
    workspace_id: UUID, event_id: UUID, db: DB, user: Actor, offset: int = Query(0, ge=0)
):
    await scoped_event(db, workspace_id, event_id, user)
    rows = (await db.execute(attendee_query(event_id).offset(offset).limit(101))).all()
    return {
        "items": [
            dict(
                id=str(ticket.id),
                name=person.display_name,
                ticket_type=name,
                status=ticket.status,
                booking_status=status,
                admitted_at=ticket.admitted_at,
            )
            for ticket, person, name, status in rows[:100]
        ],
        "next_offset": offset + 100 if len(rows) > 100 else None,
    }


@router.get("/workspaces/{workspace_id}/events/{event_id}/totals")
async def totals(workspace_id: UUID, event_id: UUID, db: DB, user: Actor):
    await scoped_event(db, workspace_id, event_id, user, owner=True)
    bookings, gross = (
        await db.execute(
            sa.select(
                sa.func.count(Booking.id), sa.func.coalesce(sa.func.sum(Booking.total_minor), 0)
            ).where(Booking.event_id == event_id, Booking.status == "confirmed")
        )
    ).one()
    tickets, admitted = (
        await db.execute(
            sa.select(sa.func.count(Ticket.id), sa.func.count(Ticket.admitted_at)).where(
                Ticket.event_id == event_id, Ticket.status == "valid"
            )
        )
    ).one()
    return dict(
        confirmed_bookings=bookings,
        valid_tickets=tickets,
        admitted=admitted,
        confirmed_revenue_minor=gross,
        currency="INR",
    )


def csv_cell(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


@router.get("/workspaces/{workspace_id}/events/{event_id}/attendees.csv")
async def export_attendees(workspace_id: UUID, event_id: UUID, db: DB, user: Actor):
    workspace, _ = await scoped_event(db, workspace_id, event_id, user, owner=True)
    rows = (await db.execute(attendee_query(event_id).limit(10001))).all()
    if len(rows) > 10000:
        raise HTTPException(409, "Export exceeds the 10,000-ticket limit")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Ticket ID",
            "Name",
            "Email",
            "Ticket type",
            "Ticket status",
            "Booking status",
            "Admitted at",
        ]
    )
    for ticket, person, name, status in rows:
        writer.writerow(
            [
                csv_cell(v)
                for v in (
                    ticket.id,
                    person.display_name,
                    person.email,
                    name,
                    ticket.status,
                    status,
                    ticket.admitted_at,
                )
            ]
        )
    audit(db, user, workspace, "attendees.exported", event_id)
    await db.commit()
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="attendees.csv"',
            "Cache-Control": "no-store",
        },
    )


class AdmissionInput(BaseModel):
    ticket_id: UUID | None = None
    qr: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def one_method(self):
        if (self.ticket_id is None) == (self.qr is None):
            raise ValueError("Provide either a QR credential or a ticket ID")
        return self


@router.post("/workspaces/{workspace_id}/events/{event_id}/check-in", dependencies=MUTATIONS)
async def admit(workspace_id: UUID, event_id: UUID, body: AdmissionInput, db: DB, user: Actor):
    workspace, event = await scoped_event(db, workspace_id, event_id, user, mutation=True)
    now = datetime.now(UTC)
    if workspace.status != "approved" or event.status != "published":
        raise HTTPException(409, "This event is unavailable for admission")
    if event.ends_at and utc(event.ends_at) <= now:
        raise HTTPException(409, "This event has ended")
    ticket_id = body.ticket_id
    if body.qr:
        try:
            prefix, raw_id, digest = body.qr.split(":")
            ticket_id = UUID(raw_id)
            if prefix != "gather" or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, "Invalid QR credential") from exc
    row = (
        await db.execute(
            sa.select(Ticket, Booking.status)
            .join(Booking, Booking.id == Ticket.booking_id)
            .where(
                Ticket.id == ticket_id,
                Ticket.event_id == event_id,
                Ticket.organizer_id == workspace_id,
            )
            .with_for_update(of=Ticket)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Ticket not found for this event")
    ticket, status = row
    if body.qr and not hmac.compare_digest(body.qr, credential(ticket)):
        raise HTTPException(404, "Ticket not found for this event")
    if ticket.status != "valid" or status != "confirmed":
        raise HTTPException(409, "This ticket is cancelled or unconfirmed")
    if ticket.admitted_at:
        return dict(
            status="already_admitted", ticket_id=str(ticket.id), admitted_at=ticket.admitted_at
        )
    ticket.admitted_at = now
    ticket.admitted_by = user.id
    db.add(
        CheckIn(ticket_id=ticket.id, event_id=event.id, organizer_id=workspace_id, actor_id=user.id)
    )
    audit(db, user, workspace, "ticket.admitted", ticket.id)
    await db.commit()
    return dict(status="admitted", ticket_id=str(ticket.id), admitted_at=now)
