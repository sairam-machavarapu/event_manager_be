"""Workspace membership is resolved for every protected request."""

import re
from typing import Annotated, Literal
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from app.auth import DB, current_user, require_same_origin
from app.models import AuditEntry, DiscoveryVersion, Membership, Organizer, User

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])
Actor = Annotated[User, Depends(current_user)]
Role = Literal["owner", "editor", "check_in"]
MUTATIONS = [Depends(require_same_origin)]


class WorkspaceInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=3, max_length=160)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a workspace name")
        return value.strip()

    @field_validator("slug")
    @classmethod
    def clean_slug(cls, value):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Use lowercase letters, numbers and single hyphens")
        return value


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    role: Role


class MemberInput(BaseModel):
    user_id: UUID
    role: Literal["editor", "check_in"]


class MemberRole(BaseModel):
    role: Literal["editor", "check_in"]


class MemberView(BaseModel):
    user_id: UUID
    display_name: str
    role: Role


def view(workspace, membership):
    return WorkspaceView(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        status=workspace.status,
        role=membership.role,
    )


async def access(db, organizer_id, user, *, mutation=False, roles=None):
    query = sa.select(Organizer).where(Organizer.id == organizer_id)
    if mutation:
        # Team changes share this lock; membership is checked after acquiring it.
        query = query.with_for_update()
    # Uploads authorize once before reading bytes and again after acquiring the
    # mutation lock. Refresh identity-map rows so revocation/suspension is current.
    workspace = await db.scalar(query.execution_options(populate_existing=True))
    membership = await db.scalar(
        sa.select(Membership)
        .where(Membership.organizer_id == organizer_id, Membership.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    if workspace is None or membership is None:
        raise HTTPException(404, "Workspace not found")
    if roles is not None and membership.role not in roles:
        raise HTTPException(403, "Your workspace role does not allow this action")
    if mutation and workspace.status == "suspended":
        raise HTTPException(403, "This workspace is suspended")
    return workspace, membership


def audit(db, user, workspace, action, target):
    db.add(AuditEntry(organizer_id=workspace.id, actor_id=user.id, action=action, target_id=target))


async def commit(db):
    try:
        await db.execute(
            sa.update(DiscoveryVersion)
            .where(DiscoveryVersion.id == 1)
            .values(revision=DiscoveryVersion.revision + 1)
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Workspace slug or membership already exists") from exc


@router.get("", response_model=list[WorkspaceView])
async def list_workspaces(db: DB, user: Actor, response: Response):
    response.headers["Cache-Control"] = "no-store"
    rows = await db.execute(
        sa.select(Organizer, Membership)
        .join(Membership, Membership.organizer_id == Organizer.id)
        .where(Membership.user_id == user.id)
        .order_by(Organizer.created_at, Organizer.id)
    )
    return [view(workspace, membership) for workspace, membership in rows.all()]


@router.post("", response_model=WorkspaceView, status_code=201, dependencies=MUTATIONS)
async def create_workspace(body: WorkspaceInput, db: DB, user: Actor):
    workspace = Organizer(name=body.name, slug=body.slug, status="pending")
    db.add(workspace)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Workspace slug already exists") from exc
    membership = Membership(organizer_id=workspace.id, user_id=user.id, role="owner")
    db.add(membership)
    audit(db, user, workspace, "workspace.created", workspace.id)
    await commit(db)
    return view(workspace, membership)


@router.get("/{organizer_id}", response_model=WorkspaceView)
async def get_workspace(organizer_id: UUID, db: DB, user: Actor):
    return view(*await access(db, organizer_id, user))


@router.patch("/{organizer_id}", response_model=WorkspaceView, dependencies=MUTATIONS)
async def update_workspace(organizer_id: UUID, body: WorkspaceInput, db: DB, user: Actor):
    workspace, membership = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    workspace.name, workspace.slug = body.name, body.slug
    audit(db, user, workspace, "workspace.updated", workspace.id)
    await commit(db)
    return view(workspace, membership)


@router.get("/{organizer_id}/members", response_model=list[MemberView])
async def list_members(organizer_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles={"owner"})
    rows = await db.execute(
        sa.select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organizer_id == organizer_id)
        .order_by(Membership.created_at)
    )
    return [
        MemberView(user_id=member.user_id, display_name=person.display_name, role=member.role)
        for member, person in rows.all()
    ]


@router.post(
    "/{organizer_id}/members", response_model=MemberView, status_code=201, dependencies=MUTATIONS
)
async def add_member(organizer_id: UUID, body: MemberInput, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    person = await db.scalar(sa.select(User).where(User.id == body.user_id))
    if person is None:
        raise HTTPException(404, "Account not found")
    db.add(Membership(organizer_id=organizer_id, user_id=person.id, role=body.role))
    audit(db, user, workspace, f"member.added.{body.role}", person.id)
    await commit(db)
    return MemberView(user_id=person.id, display_name=person.display_name, role=body.role)


async def team_target(db, organizer_id, user_id):
    member = await db.scalar(
        sa.select(Membership).where(
            Membership.organizer_id == organizer_id, Membership.user_id == user_id
        )
    )
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == "owner":
        raise HTTPException(409, "Workspace ownership cannot be changed here")
    return member


@router.patch("/{organizer_id}/members/{user_id}", status_code=204, dependencies=MUTATIONS)
async def change_role(organizer_id: UUID, user_id: UUID, body: MemberRole, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    member = await team_target(db, organizer_id, user_id)
    member.role = body.role
    audit(db, user, workspace, f"member.role_changed.{body.role}", user_id)
    await commit(db)


@router.delete("/{organizer_id}/members/{user_id}", status_code=204, dependencies=MUTATIONS)
async def remove_member(organizer_id: UUID, user_id: UUID, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, mutation=True, roles={"owner"})
    await team_target(db, organizer_id, user_id)
    await db.execute(
        sa.delete(Membership).where(
            Membership.organizer_id == organizer_id, Membership.user_id == user_id
        )
    )
    audit(db, user, workspace, "member.removed", user_id)
    await commit(db)
