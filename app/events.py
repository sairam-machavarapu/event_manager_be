"""Workspace-scoped event drafts. Publication is a separate lifecycle action."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.auth import DB
from app.config import get_settings
from app.models import Booking, Event, EventMedia, Outbox, PaymentAttempt, Ticket, TicketType, User
from app.notifications import email_payload
from app.refunds import queue_refund
from app.workspaces import MUTATIONS, Actor, access, audit, commit

router = APIRouter(prefix="/api/v1/workspaces/{organizer_id}/events", tags=["event drafts"])
EDITORS = {"owner", "editor"}


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    format: Literal["in_person", "online"] = "in_person"
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    online_url: HttpUrl | None = None
    cover_url: HttpUrl | None = None
    category: (
        Literal["Music", "Workshops", "Community", "Talks", "Food & drink", "Outdoors", "Other"]
        | None
    ) = None

    @field_validator("cover_url")
    @classmethod
    def secure_cover(cls, value):
        if value is not None and (value.scheme != "https" or value.username or value.password):
            raise ValueError("Use an HTTPS image URL without credentials")
        return value

    @field_validator("title", "description", "venue", "city")
    @classmethod
    def clean_text(cls, value):
        return value.strip() or None if value is not None else None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a named IANA timezone") from exc
        return value

    @field_validator("starts_at", "ends_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Include a timezone offset")
            return value.astimezone(UTC)
        return value


class DraftView(DraftInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organizer_id: UUID
    slug: str
    status: str
    cancellation_reason: str | None = None
    cancelled_at: datetime | None = None
    revision: int


def values(body):
    result = body.model_dump(exclude_unset=True)
    for name in ("online_url", "cover_url"):
        if name in result and result[name] is not None:
            result[name] = str(result[name])
    return result


def validate_timing(start, end):
    # SQLite's test adapter returns naive UTC values; PostgreSQL preserves offsets.
    def utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    if start and end and utc(end) <= utc(start):
        raise HTTPException(422, "End time must be after start time")


def view(event):
    data = {name: getattr(event, name) for name in DraftView.model_fields}
    for name in ("starts_at", "ends_at"):
        if data[name] is not None and data[name].tzinfo is None:
            data[name] = data[name].replace(tzinfo=UTC)
    return DraftView(**data)


@router.get("", response_model=list[DraftView])
async def list_events(organizer_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles=EDITORS)
    rows = await db.execute(
        sa.select(Event)
        .where(Event.organizer_id == organizer_id)
        .order_by(Event.created_at.desc(), Event.id)
    )
    return [view(event) for event in rows.scalars()]


async def target(db, organizer_id, event_id):
    event = await db.scalar(
        sa.select(Event).where(Event.organizer_id == organizer_id, Event.id == event_id)
    )
    if event is None:
        raise HTTPException(404, "Event not found")
    return event


@router.get("/{event_id}", response_model=DraftView)
async def get_event(organizer_id: UUID, event_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles=EDITORS)
    return view(await target(db, organizer_id, event_id))


@router.post("", response_model=DraftView, status_code=201, dependencies=MUTATIONS)
async def create_event(organizer_id: UUID, body: DraftInput, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles=EDITORS)
    validate_timing(body.starts_at, body.ends_at)
    event = Event(
        organizer_id=organizer_id, slug=f"event-{uuid4().hex}", status="draft", **values(body)
    )
    db.add(event)
    await db.flush()
    audit(db, user, workspace, "event.created", event.id)
    await commit(db)
    return view(event)


class PublicationIssue(BaseModel):
    field: str
    message: str


class PublicationView(BaseModel):
    ready: bool
    event_revision: int
    issues: list[PublicationIssue]


def revision_guard(event, if_match):
    if if_match is not None and if_match != f'"{event.revision}"':
        raise HTTPException(
            412, "This event changed in another session. Reload it before continuing."
        )


async def publication_issues(db, event):
    organizer_id, event_id = event.organizer_id, event.id
    issues = []
    missing = [
        name
        for name in ("title", "description", "starts_at", "ends_at", "category")
        if not getattr(event, name)
    ]
    location = ("venue", "city") if event.format == "in_person" else ("online_url",)
    missing.extend(name for name in location if not getattr(event, name))
    poster = await db.scalar(
        sa.select(EventMedia.id)
        .where(
            EventMedia.event_id == event_id,
            EventMedia.organizer_id == organizer_id,
            EventMedia.kind == "poster",
            EventMedia.status == "ready",
        )
        .limit(1)
    )
    if not event.cover_url and poster is None:
        missing.append("cover image")
    issues.extend(
        PublicationIssue(field=name, message=f"Add {name.replace('_', ' ')}") for name in missing
    )
    start = event.starts_at
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if start is not None and start <= datetime.now(UTC):
        issues.append(
            PublicationIssue(field="starts_at", message="Start time must be in the future")
        )
    if event.starts_at and event.ends_at:
        try:
            validate_timing(event.starts_at, event.ends_at)
        except HTTPException as exc:
            issues.append(PublicationIssue(field="ends_at", message=exc.detail))
    available = await db.scalar(
        sa.select(TicketType.id)
        .where(
            TicketType.organizer_id == organizer_id,
            TicketType.event_id == event_id,
            TicketType.capacity > 0,
        )
        .limit(1)
    )
    if available is None:
        issues.append(
            PublicationIssue(field="tickets", message="Add a ticket type with positive capacity")
        )
    return issues


@router.get("/{event_id}/publication", response_model=PublicationView)
async def publication_readiness(organizer_id: UUID, event_id: UUID, db: DB, user: Actor):
    workspace, membership = await access(db, organizer_id, user, roles=EDITORS)
    event = await target(db, organizer_id, event_id)
    issues = await publication_issues(db, event)
    if workspace.status != "approved":
        issues.append(PublicationIssue(field="workspace", message="Workspace approval is required"))
    if membership.role != "owner":
        issues.append(
            PublicationIssue(field="role", message="Only the workspace owner can publish")
        )
    if event.status != "draft":
        issues.append(PublicationIssue(field="status", message=f"This event is {event.status}"))
    return PublicationView(ready=not issues, event_revision=event.revision, issues=issues)


@router.post("/{event_id}/publish", response_model=DraftView, dependencies=MUTATIONS)
async def publish_event(
    organizer_id: UUID,
    event_id: UUID,
    db: DB,
    user: Actor,
    if_match: str | None = Header(default=None),
):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    event = await target(db, organizer_id, event_id)
    revision_guard(event, if_match)
    if workspace.status != "approved":
        raise HTTPException(403, "Your workspace must be approved before publishing")
    if event.status != "draft":
        raise HTTPException(409, "Only draft events can be published")
    issues = await publication_issues(db, event)
    if issues:
        raise HTTPException(422, "; ".join(issue.message for issue in issues))
    event.status = "published"
    event.revision += 1
    from app.engagement import queue_publication

    await queue_publication(db, event)
    audit(db, user, workspace, "event.published", event.id)
    await commit(db)
    return view(event)


class CancellationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_text(cls, value):
        if not value.strip():
            raise ValueError("Enter a cancellation reason")
        return value.strip()


@router.post("/{event_id}/cancel", response_model=DraftView, dependencies=MUTATIONS)
async def cancel_event(
    organizer_id: UUID, event_id: UUID, body: CancellationInput, db: DB, user: Actor
):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    event = await target(db, organizer_id, event_id)
    return await cancel_locked(db, user, workspace, event, body)


async def cancel_locked(db, user, workspace, event, body):
    organizer_id, event_id = workspace.id, event.id
    if event.status == "cancelled":
        raise HTTPException(409, "This event is already cancelled")
    bookings = (
        await db.execute(
            sa.select(Booking, User.email)
            .join(User, User.id == Booking.user_id)
            .where(
                Booking.event_id == event_id,
                Booking.organizer_id == organizer_id,
                Booking.status.in_(["pending", "confirmed"]),
            )
        )
    ).all()
    for booking, _email in bookings:
        if booking.status == "confirmed" and booking.total_minor > 0:
            attempts = (
                (
                    await db.execute(
                        sa.select(PaymentAttempt).where(
                            PaymentAttempt.booking_id == booking.id, PaymentAttempt.status == "paid"
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(attempts) != 1 or attempts[0].amount_minor != booking.total_minor:
                raise HTTPException(
                    409, "Paid booking requires payment reconciliation before cancellation"
                )
            await queue_refund(db, attempts[0], "event_cancelled")
    for booking, email in bookings:
        was_paid = booking.status == "confirmed" and booking.total_minor > 0
        booking.status = "cancelled"
        db.add(
            Outbox(
                kind="email",
                organizer_id=organizer_id,
                booking_id=booking.id,
                topic="cancellation",
                payload=email_payload(
                    email,
                    "Event cancelled",
                    f"{event.title or 'Your event'} has been cancelled: {body.reason}"
                    + (
                        ". Your full sandbox refund has been queued; "
                        "completion will be confirmed separately."
                        if was_paid
                        else ""
                    ),
                    link=f"{get_settings().auth_origin}/orders/{booking.id}/confirmation",
                ),
            )
        )
    await db.execute(
        sa.update(Ticket)
        .where(Ticket.event_id == event_id, Ticket.organizer_id == organizer_id)
        .values(status="cancelled")
    )
    event.status = "cancelled"
    event.revision += 1
    event.cancellation_reason = body.reason
    event.cancelled_at = datetime.now(UTC)
    audit(db, user, workspace, "event.cancelled", event.id)
    await commit(db)
    return view(event)


@router.patch("/{event_id}", response_model=DraftView, dependencies=MUTATIONS)
async def update_event(
    organizer_id: UUID,
    event_id: UUID,
    body: DraftInput,
    db: DB,
    user: Actor,
    if_match: str | None = Header(default=None),
):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles=EDITORS)
    event = await target(db, organizer_id, event_id)
    revision_guard(event, if_match)
    if event.status != "draft":
        raise HTTPException(409, "Only draft events can be edited here")
    updates = values(body)
    validate_timing(
        updates.get("starts_at", event.starts_at), updates.get("ends_at", event.ends_at)
    )
    for name, value in updates.items():
        setattr(event, name, value)
    event.revision += 1
    audit(db, user, workspace, "event.updated", event.id)
    await commit(db)
    return view(event)
