"""Platform administration, distinct from workspace membership."""

from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import DB, current_user, rate_limit, require_same_origin
from app.events import CancellationInput, cancel_locked
from app.models import AuditEntry, Event, EventReport, Organizer, User
from app.workspaces import audit, commit

router = APIRouter(prefix="/api/v1", tags=["administration"])


async def administrator(user: Annotated[User, Depends(current_user)]):
    if not user.is_admin:
        raise HTTPException(403, "Administrator access required")
    return user


Admin = Annotated[User, Depends(administrator)]
Actor = Annotated[User, Depends(current_user)]
MUTATIONS = [Depends(require_same_origin)]
Offset = Annotated[int, Query(ge=0, le=1000000)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value):
        if not value.strip():
            raise ValueError("Enter a reason")
        return value.strip()


class WorkspaceDecision(Decision):
    status: Literal["approved", "suspended"]


class ReportDecision(Decision):
    status: Literal["resolved", "dismissed"]


def workspace_view(row):
    return dict(id=row.id, name=row.name, slug=row.slug, status=row.status)


@router.get("/admin/workspaces")
async def workspaces(db: DB, user: Admin, offset: Offset = 0):
    rows = (
        (
            await db.execute(
                sa.select(Organizer)
                .order_by(Organizer.created_at.desc(), Organizer.id.desc())
                .offset(offset)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    return dict(
        items=[workspace_view(row) for row in rows[:100]],
        next_offset=offset + 100 if len(rows) > 100 else None,
    )


@router.patch("/admin/workspaces/{workspace_id}", dependencies=MUTATIONS)
async def decide_workspace(workspace_id: UUID, body: WorkspaceDecision, db: DB, user: Admin):
    row = await db.scalar(
        sa.select(Organizer).where(Organizer.id == workspace_id).with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Workspace not found")
    if row.status == body.status:
        raise HTTPException(409, "Workspace already has this status")
    row.status = body.status
    db.add(
        AuditEntry(
            organizer_id=row.id,
            actor_id=user.id,
            action=f"admin.workspace.{body.status}",
            target_id=row.id,
            reason=body.reason,
        )
    )
    await commit(db)
    return workspace_view(row)


@router.get("/admin/events")
async def events(db: DB, user: Admin, offset: Offset = 0):
    rows = (
        (
            await db.execute(
                sa.select(Event)
                .order_by(Event.created_at.desc(), Event.id.desc())
                .offset(offset)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    return dict(
        items=[
            dict(id=r.id, organizer_id=r.organizer_id, title=r.title, slug=r.slug, status=r.status)
            for r in rows[:100]
        ],
        next_offset=offset + 100 if len(rows) > 100 else None,
    )


@router.post("/admin/events/{event_id}/cancel", dependencies=MUTATIONS)
async def moderate_event(event_id: UUID, body: CancellationInput, db: DB, user: Admin):
    organizer_id = await db.scalar(sa.select(Event.organizer_id).where(Event.id == event_id))
    if organizer_id is None:
        raise HTTPException(404, "Event not found")
    workspace = await db.scalar(
        sa.select(Organizer).where(Organizer.id == organizer_id).with_for_update()
    )
    event = await db.scalar(sa.select(Event).where(Event.id == event_id).with_for_update())
    db.add(
        AuditEntry(
            organizer_id=workspace.id,
            actor_id=user.id,
            action="admin.event.cancelled",
            target_id=event.id,
            reason=body.reason,
        )
    )
    return await cancel_locked(db, user, workspace, event, body)


@router.post(
    "/events/{event_id}/reports", status_code=201, dependencies=[*MUTATIONS, Depends(rate_limit)]
)
async def report_event(event_id: UUID, body: Decision, db: DB, user: Actor):
    organizer_id = await db.scalar(sa.select(Event.organizer_id).where(Event.id == event_id))
    if organizer_id is None:
        raise HTTPException(404, "Event not found")
    await db.scalar(sa.select(Organizer).where(Organizer.id == organizer_id).with_for_update())
    event = await db.scalar(
        sa.select(Event)
        .join(Organizer)
        .where(Event.id == event_id, Event.status == "published", Organizer.status == "approved")
        .with_for_update()
    )
    if event is None:
        raise HTTPException(404, "Event not found")
    if await db.scalar(
        sa.select(EventReport.id).where(
            EventReport.event_id == event.id,
            EventReport.reporter_id == user.id,
            EventReport.status == "open",
        )
    ):
        raise HTTPException(409, "You already have an open report for this event")
    row = EventReport(
        event_id=event.id, organizer_id=event.organizer_id, reporter_id=user.id, reason=body.reason
    )
    db.add(row)
    audit(db, user, Organizer(id=event.organizer_id), "event.reported", event.id)
    await db.commit()
    return dict(id=row.id, status=row.status)


@router.get("/admin/reports")
async def reports(db: DB, user: Admin, offset: Offset = 0):
    rows = (
        (
            await db.execute(
                sa.select(EventReport)
                .order_by(EventReport.created_at.desc(), EventReport.id.desc())
                .offset(offset)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    return dict(
        items=[
            dict(
                id=r.id,
                event_id=r.event_id,
                organizer_id=r.organizer_id,
                reason=r.reason,
                status=r.status,
                resolution=r.resolution,
            )
            for r in rows[:100]
        ],
        next_offset=offset + 100 if len(rows) > 100 else None,
    )


@router.patch("/admin/reports/{report_id}", dependencies=MUTATIONS)
async def resolve_report(report_id: UUID, body: ReportDecision, db: DB, user: Admin):
    row = await db.scalar(
        sa.select(EventReport).where(EventReport.id == report_id).with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Report not found")
    if row.status != "open":
        raise HTTPException(409, "Report already reviewed")
    row.status, row.resolution, row.resolved_by = body.status, body.reason, user.id
    audit(db, user, Organizer(id=row.organizer_id), f"admin.report.{body.status}", row.id)
    await db.commit()
    return dict(id=row.id, status=row.status)


@router.get("/admin/audit")
async def history(db: DB, user: Admin, offset: Offset = 0):
    rows = (
        (
            await db.execute(
                sa.select(AuditEntry)
                .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
                .offset(offset)
                .limit(101)
            )
        )
        .scalars()
        .all()
    )
    return dict(
        items=[
            dict(
                id=r.id,
                organizer_id=r.organizer_id,
                actor_id=r.actor_id,
                action=r.action,
                target_id=r.target_id,
                reason=r.reason,
                created_at=r.created_at,
            )
            for r in rows[:100]
        ],
        next_offset=offset + 100 if len(rows) > 100 else None,
    )
