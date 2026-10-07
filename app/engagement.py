"""Private engagement; notification alerts never reserve inventory."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import DB, current_user, require_same_origin
from app.bookings import lock_event, lock_user, used_inventory, utc
from app.config import get_settings
from app.models import (
    Event,
    Organizer,
    OrganizerFollow,
    Outbox,
    SavedEvent,
    TicketType,
    User,
    WaitlistEntry,
)
from app.notifications import email_payload

router = APIRouter(prefix="/api/v1", tags=["engagement"])
Actor = Annotated[User, Depends(current_user)]
MUTATIONS = [Depends(require_same_origin)]


def available(event, host, now):
    return (
        host.status == "approved"
        and event.status == "published"
        and event.starts_at is not None
        and utc(event.starts_at) > now
    )


def sales_open(ticket, now):
    return (not ticket.sales_start or utc(ticket.sales_start) <= now) and (
        not ticket.sales_end or utc(ticket.sales_end) > now
    )


async def public_event(db, event_id):
    event, host = await lock_event(db, event_id)
    if not available(event, host, datetime.now(UTC)):
        raise HTTPException(404, "Upcoming event not found")
    return event, host


@router.get("/engagement/events/{event_id}")
async def state(event_id: UUID, db: DB, user: Actor):
    # Private state must not disclose draft/suspended event existence.
    event, host = await public_event(db, event_id)
    saved = await db.scalar(
        sa.select(SavedEvent.id).where(
            SavedEvent.user_id == user.id, SavedEvent.event_id == event.id
        )
    )
    follow = await db.scalar(
        sa.select(OrganizerFollow.id).where(
            OrganizerFollow.user_id == user.id, OrganizerFollow.organizer_id == host.id
        )
    )
    entries = (
        (
            await db.execute(
                sa.select(WaitlistEntry)
                .join(TicketType)
                .where(WaitlistEntry.user_id == user.id, TicketType.event_id == event.id)
            )
        )
        .scalars()
        .all()
    )
    return dict(
        saved=bool(saved),
        following=bool(follow),
        organizer_id=host.id,
        waitlists=[dict(ticket_type_id=r.ticket_type_id, status=r.status) for r in entries],
    )


@router.put("/engagement/events/{event_id}/save", dependencies=MUTATIONS)
async def save(event_id: UUID, db: DB, user: Actor):
    await lock_user(db, user)
    await public_event(db, event_id)
    if not await db.scalar(
        sa.select(SavedEvent.id).where(
            SavedEvent.user_id == user.id, SavedEvent.event_id == event_id
        )
    ):
        db.add(SavedEvent(user_id=user.id, event_id=event_id))
    await db.commit()
    return {"saved": True}


@router.delete("/engagement/events/{event_id}/save", dependencies=MUTATIONS)
async def unsave(event_id: UUID, db: DB, user: Actor):
    await lock_user(db, user)
    await db.execute(
        sa.delete(SavedEvent).where(SavedEvent.user_id == user.id, SavedEvent.event_id == event_id)
    )
    await db.commit()
    return {"saved": False}


@router.put("/engagement/events/{event_id}/follow", dependencies=MUTATIONS)
async def follow(event_id: UUID, db: DB, user: Actor):
    actor = await lock_user(db, user)
    if actor.verified_at is None:
        raise HTTPException(403, "Verify your email before following organisers")
    _, host = await public_event(db, event_id)
    if not await db.scalar(
        sa.select(OrganizerFollow.id).where(
            OrganizerFollow.user_id == user.id, OrganizerFollow.organizer_id == host.id
        )
    ):
        db.add(OrganizerFollow(user_id=user.id, organizer_id=host.id))
    await db.commit()
    return {"following": True}


@router.delete("/engagement/follows/{organizer_id}", dependencies=MUTATIONS)
async def unfollow(organizer_id: UUID, db: DB, user: Actor):
    await lock_user(db, user)
    # Publication and delivery use the workspace lock, serializing unsubscribe.
    await db.scalar(sa.select(Organizer).where(Organizer.id == organizer_id).with_for_update())
    await db.execute(
        sa.delete(OrganizerFollow).where(
            OrganizerFollow.user_id == user.id, OrganizerFollow.organizer_id == organizer_id
        )
    )
    await db.commit()
    return {"following": False}


@router.put("/engagement/waitlists/{ticket_type_id}", dependencies=MUTATIONS)
async def join_waitlist(ticket_type_id: UUID, db: DB, user: Actor):
    actor = await lock_user(db, user)
    if actor.verified_at is None:
        raise HTTPException(403, "Verify your email before joining a waitlist")
    ticket = await db.scalar(sa.select(TicketType).where(TicketType.id == ticket_type_id))
    if ticket is None:
        raise HTTPException(404, "Ticket type not found")
    event, _ = await public_event(db, ticket.event_id)
    ticket = await db.scalar(
        sa.select(TicketType)
        .where(TicketType.id == ticket_type_id)
        .execution_options(populate_existing=True)
    )
    row = await db.scalar(
        sa.select(WaitlistEntry).where(
            WaitlistEntry.user_id == user.id, WaitlistEntry.ticket_type_id == ticket.id
        )
    )
    if row is None:
        now = datetime.now(UTC)
        used = await used_inventory(db, event.id, now)
        if not sales_open(ticket, now) or ticket.capacity - used.get(ticket.id, 0) > 0:
            raise HTTPException(409, "Waitlists are for sold-out tickets during their sales window")
        row = WaitlistEntry(user_id=user.id, ticket_type_id=ticket.id)
        db.add(row)
    await db.commit()
    return dict(status=row.status)


@router.delete("/engagement/waitlists/{ticket_type_id}", dependencies=MUTATIONS)
async def leave_waitlist(ticket_type_id: UUID, db: DB, user: Actor):
    await lock_user(db, user)
    ticket = await db.scalar(sa.select(TicketType).where(TicketType.id == ticket_type_id))
    if ticket:
        await lock_event(db, ticket.event_id)
    await db.execute(
        sa.delete(WaitlistEntry).where(
            WaitlistEntry.user_id == user.id, WaitlistEntry.ticket_type_id == ticket_type_id
        )
    )
    await db.commit()
    return {"status": "left"}


@router.get("/engagement/library")
async def library(
    db: DB,
    user: Actor,
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    follows_offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    waitlists_offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
):
    rows = (
        await db.execute(
            sa.select(Event, Organizer)
            .join(SavedEvent)
            .join(Organizer, Organizer.id == Event.organizer_id)
            .where(
                SavedEvent.user_id == user.id,
                Organizer.status == "approved",
                Event.status.in_(["published", "cancelled"]),
            )
            .order_by(SavedEvent.created_at.desc(), SavedEvent.id.desc())
            .offset(offset)
            .limit(51)
        )
    ).all()
    follows = (
        (
            await db.execute(
                sa.select(Organizer)
                .join(OrganizerFollow)
                .where(OrganizerFollow.user_id == user.id)
                .order_by(Organizer.name, Organizer.id)
                .offset(follows_offset)
                .limit(51)
            )
        )
        .scalars()
        .all()
    )
    waitlists = (
        await db.execute(
            sa.select(WaitlistEntry, TicketType, Event, Organizer)
            .join(TicketType, TicketType.id == WaitlistEntry.ticket_type_id)
            .join(Event, Event.id == TicketType.event_id)
            .join(Organizer, Organizer.id == Event.organizer_id)
            .where(WaitlistEntry.user_id == user.id)
            .order_by(WaitlistEntry.created_at.desc(), WaitlistEntry.id.desc())
            .offset(waitlists_offset)
            .limit(51)
        )
    ).all()
    return dict(
        events=[
            dict(
                id=e.id,
                slug=e.slug,
                title=e.title,
                status=e.status,
                organizer=h.name,
                starts_at=e.starts_at,
            )
            for e, h in rows[:50]
        ],
        next_offset=offset + 50 if len(rows) > 50 else None,
        follows=[dict(id=h.id, name=h.name, status=h.status) for h in follows[:50]],
        follows_next=follows_offset + 50 if len(follows) > 50 else None,
        waitlists=[
            dict(
                ticket_type_id=t.id,
                ticket=t.name,
                title=e.title,
                slug=e.slug if h.status == "approved" else None,
                status=w.status,
                event_status=e.status,
            )
            for w, t, e, h in waitlists[:50]
        ],
        waitlists_next=waitlists_offset + 50 if len(waitlists) > 50 else None,
    )


async def queue_publication(db, event):
    db.add(
        Outbox(
            kind="publication_fanout",
            organizer_id=event.organizer_id,
            topic="publication",
            dedupe_key=f"publication:{event.id}",
            payload={"event_id": str(event.id), "cursor": None},
        )
    )


async def publication_batch(db, message):
    event, host = await lock_event(db, UUID(message.payload["event_id"]))
    if not available(event, host, datetime.now(UTC)):
        message.skipped_at = datetime.now(UTC)
        return True
    query = sa.select(OrganizerFollow).where(
        OrganizerFollow.organizer_id == host.id, OrganizerFollow.created_at <= message.created_at
    )
    if message.payload.get("cursor"):
        query = query.where(OrganizerFollow.id > UUID(message.payload["cursor"]))
    rows = (await db.execute(query.order_by(OrganizerFollow.id).limit(101))).scalars().all()
    for row in rows[:100]:
        key = f"publication:{event.id}:{row.id}"
        if not await db.scalar(sa.select(Outbox.id).where(Outbox.dedupe_key == key)):
            db.add(
                Outbox(
                    kind="engagement",
                    organizer_id=host.id,
                    topic="publication",
                    dedupe_key=key,
                    payload={"event_id": str(event.id), "follow_id": str(row.id)},
                )
            )
    if len(rows) > 100:
        message.payload = {**message.payload, "cursor": str(rows[99].id)}
        message.available_at = datetime.now(UTC) + timedelta(seconds=10)
        return False
    return True


async def recover_waitlists(factory):
    now = datetime.now(UTC)
    async with factory() as db:
        ids = list(
            (
                await db.execute(
                    sa.select(WaitlistEntry.id)
                    .where(WaitlistEntry.status == "waiting", WaitlistEntry.next_check_at <= now)
                    .order_by(WaitlistEntry.next_check_at, WaitlistEntry.id)
                    .limit(100)
                )
            ).scalars()
        )
    for entry_id in ids:
        async with factory() as db, db.begin():
            ticket = await db.scalar(
                sa.select(TicketType).join(WaitlistEntry).where(WaitlistEntry.id == entry_id)
            )
            if ticket is None:
                continue
            event, host = await lock_event(db, ticket.event_id)
            ticket = await db.scalar(
                sa.select(TicketType)
                .where(TicketType.id == ticket.id)
                .execution_options(populate_existing=True)
            )
            entry = await db.scalar(
                sa.select(WaitlistEntry).where(WaitlistEntry.id == entry_id).with_for_update()
            )
            if entry is None or entry.status != "waiting":
                continue
            entry.next_check_at = now + timedelta(seconds=60)
            if not available(event, host, now) or not sales_open(ticket, now):
                continue
            used = await used_inventory(db, event.id, now)
            if ticket.capacity <= used.get(ticket.id, 0):
                continue
            entry.status = "notified"
            db.add(
                Outbox(
                    kind="engagement",
                    organizer_id=host.id,
                    topic="waitlist",
                    dedupe_key=f"waitlist:{entry.id}",
                    payload={"event_id": str(event.id), "entry_id": str(entry.id)},
                )
            )


async def engagement_payload(db, message):
    event, host = await lock_event(db, UUID(message.payload["event_id"]))
    now = datetime.now(UTC)
    if not available(event, host, now):
        return None
    if message.topic == "publication":
        follow = await db.scalar(
            sa.select(OrganizerFollow).where(
                OrganizerFollow.id == UUID(message.payload["follow_id"]),
                OrganizerFollow.organizer_id == host.id,
            )
        )
        if follow is None:
            return None
        user_id = follow.user_id
        subject = f"New event from {host.name}"
        text = f"{host.name} has published {event.title}."
    else:
        entry = await db.scalar(
            sa.select(WaitlistEntry).where(WaitlistEntry.id == UUID(message.payload["entry_id"]))
        )
        if entry is None:
            return None
        ticket = await db.scalar(sa.select(TicketType).where(TicketType.id == entry.ticket_type_id))
        used = await used_inventory(db, event.id, now)
        if (
            ticket is None
            or not sales_open(ticket, now)
            or ticket.capacity <= used.get(ticket.id, 0)
        ):
            return None
        user_id = entry.user_id
        subject = f"Tickets available for {event.title}"
        text = (
            f"{ticket.name} tickets are available. "
            "This alert does not reserve a ticket; availability may change."
        )
    user = await db.scalar(sa.select(User).where(User.id == user_id))
    if user is None or user.verified_at is None:
        return None
    return email_payload(
        user.email, subject, text, link=f"{get_settings().auth_origin}/events/{event.slug}"
    )
