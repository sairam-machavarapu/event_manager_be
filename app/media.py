from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import DB
from app.events import EDITORS, target
from app.models import EventMedia, Outbox
from app.storage import MAX_BYTES, delete_object, public_url, put_object, validate_image
from app.workspaces import MUTATIONS, Actor, access, audit, commit

router = APIRouter(
    prefix="/api/v1/workspaces/{organizer_id}/events/{event_id}/media", tags=["event media"]
)


class MediaView(BaseModel):
    id: UUID
    kind: str
    status: str
    alt_text: str
    image_url: str | None
    thumbnail_url: str | None
    error: str | None


def view(row):
    return MediaView(
        id=row.id,
        kind=row.kind,
        status=row.status,
        alt_text=row.alt_text,
        image_url=public_url(row.image_key),
        thumbnail_url=public_url(row.thumbnail_key),
        error=row.error,
    )


@router.get("", response_model=list[MediaView])
async def list_media(organizer_id: UUID, event_id: UUID, db: DB, user: Actor):
    await access(db, organizer_id, user, roles=EDITORS)
    await target(db, organizer_id, event_id)
    rows = await db.execute(
        sa.select(EventMedia)
        .where(EventMedia.organizer_id == organizer_id, EventMedia.event_id == event_id)
        .order_by(EventMedia.created_at, EventMedia.id)
    )
    return [view(row) for row in rows.scalars()]


@router.post("", status_code=202, response_model=MediaView, dependencies=MUTATIONS)
async def upload_media(
    organizer_id: UUID,
    event_id: UUID,
    request: Request,
    db: DB,
    user: Actor,
    kind: Literal["poster", "gallery"],
    alt_text: str,
):
    # Read a bounded stream before taking the workspace lock.
    if not 1 <= len(alt_text.strip()) <= 300:
        raise HTTPException(422, "Describe the image in 1–300 characters")
    await access(db, organizer_id, user, roles=EDITORS)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise HTTPException(413, "Upload an image smaller than 8 MiB")
    try:
        await run_in_threadpool(validate_image, bytes(content))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    workspace, _ = await access(db, organizer_id, user, roles=EDITORS, mutation=True)
    event = await target(db, organizer_id, event_id)
    if event.status != "draft":
        raise HTTPException(409, "Only draft media can be changed")
    count = await db.scalar(
        sa.select(sa.func.count()).select_from(EventMedia).where(EventMedia.event_id == event_id)
    )
    if count >= 20:
        raise HTTPException(422, "Remove an image before adding more (20 maximum)")
    media_id = uuid4()
    key = f"private/{organizer_id}/{event_id}/{media_id}"
    try:
        await run_in_threadpool(put_object, key, bytes(content))
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(503, "Image storage is temporarily unavailable") from exc
    row = EventMedia(
        id=media_id,
        organizer_id=organizer_id,
        event_id=event_id,
        kind=kind,
        source_key=key,
        alt_text=alt_text.strip(),
        status="processing",
    )
    db.add(row)
    db.add(Outbox(kind="image", payload={"media_id": str(media_id)}))
    audit(db, user, workspace, "media.uploaded", media_id)
    try:
        await commit(db)
    except Exception:
        # Best effort cleanup; originals also have a storage lifecycle rule.
        try:
            await run_in_threadpool(delete_object, key)
        except BotoCoreError, ClientError:
            pass
        raise
    return view(row)


@router.delete("/{media_id}", status_code=204, dependencies=MUTATIONS)
async def remove_media(organizer_id: UUID, event_id: UUID, media_id: UUID, db: DB, user: Actor):
    workspace, _ = await access(db, organizer_id, user, roles=EDITORS, mutation=True)
    event = await target(db, organizer_id, event_id)
    if event.status != "draft":
        raise HTTPException(409, "Only draft media can be changed")
    row = await db.scalar(
        sa.select(EventMedia)
        .where(
            EventMedia.id == media_id,
            EventMedia.event_id == event_id,
            EventMedia.organizer_id == organizer_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Image not found")
    for key in (row.source_key, row.image_key, row.thumbnail_key):
        if key:
            db.add(Outbox(kind="delete_image", payload={"key": key}))
    await db.execute(sa.delete(EventMedia).where(EventMedia.id == media_id))
    audit(db, user, workspace, "media.removed", media_id)
    await commit(db)
