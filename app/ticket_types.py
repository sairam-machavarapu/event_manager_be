"""Private ticket configuration for event drafts."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import DB
from app.events import EDITORS, target
from app.models import TicketType
from app.workspaces import MUTATIONS, Actor, access, audit, commit

router = APIRouter(
    prefix="/api/v1/workspaces/{organizer_id}/events/{event_id}/ticket-types",
    tags=["ticket types"],
)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    price_minor: int = Field(strict=True, ge=0, le=100_000_000)
    currency: Literal["INR"] = "INR"
    capacity: int = Field(strict=True, ge=0, le=2_147_483_647)
    sales_start: datetime | None = None
    sales_end: datetime | None = None
    per_order_limit: int = Field(default=10, strict=True, ge=1, le=1000)

    @field_validator("sales_start", "sales_end")
    @classmethod
    def aware_sales(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Include a timezone offset")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def sales_window(self):
        if self.sales_start and self.sales_end and self.sales_end <= self.sales_start:
            raise ValueError("Sales end must follow sales start")
        return self

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a ticket name")
        return value.strip()


class TicketView(TicketInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    event_id: UUID
    organizer_id: UUID

    @field_validator("sales_start", "sales_end", mode="before")
    @classmethod
    def sqlite_utc(cls, value):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


async def draft_access(db, organizer_id, event_id, user, *, mutation=False):
    workspace, _ = await access(db, organizer_id, user, roles=EDITORS, mutation=mutation)
    event = await target(db, organizer_id, event_id)
    if mutation and event.status != "draft":
        raise HTTPException(409, "Only draft ticket types can be changed")
    return workspace


@router.get("", response_model=list[TicketView])
async def list_types(organizer_id: UUID, event_id: UUID, db: DB, user: Actor):
    await draft_access(db, organizer_id, event_id, user)
    rows = await db.execute(
        sa.select(TicketType)
        .where(TicketType.organizer_id == organizer_id, TicketType.event_id == event_id)
        .order_by(TicketType.created_at, TicketType.id)
    )
    return list(rows.scalars())


@router.post("", response_model=TicketView, status_code=201, dependencies=MUTATIONS)
async def create_type(organizer_id: UUID, event_id: UUID, body: TicketInput, db: DB, user: Actor):
    workspace = await draft_access(db, organizer_id, event_id, user, mutation=True)
    ticket = TicketType(organizer_id=organizer_id, event_id=event_id, **body.model_dump())
    db.add(ticket)
    await db.flush()
    audit(db, user, workspace, "ticket_type.created", ticket.id)
    await commit(db)
    return ticket


@router.put("/{ticket_type_id}", response_model=TicketView, dependencies=MUTATIONS)
async def update_type(
    organizer_id: UUID,
    event_id: UUID,
    ticket_type_id: UUID,
    body: TicketInput,
    db: DB,
    user: Actor,
):
    workspace = await draft_access(db, organizer_id, event_id, user, mutation=True)
    ticket = await db.scalar(
        sa.select(TicketType).where(
            TicketType.organizer_id == organizer_id,
            TicketType.event_id == event_id,
            TicketType.id == ticket_type_id,
        )
    )
    if ticket is None:
        raise HTTPException(404, "Ticket type not found")
    for name, value in body.model_dump().items():
        setattr(ticket, name, value)
    audit(db, user, workspace, "ticket_type.updated", ticket.id)
    await commit(db)
    return ticket
