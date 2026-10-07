import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import UUID

import sqlalchemy as sa
from celery import Celery
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.concurrency import run_in_threadpool

from app.bookings import expire_reservations
from app.config import get_settings
from app.models import EventMedia, Organizer, Outbox
from app.notifications import recover_reminders, reminder_payload
from app.refunds import process_refund, recover_refunds
from app.storage import delete_object, get_object, image_variants, put_object

celery_app = Celery("gather", broker=get_settings().celery_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    beat_schedule={
        "deliver-outbox": {"task": "gather.drain_outbox", "schedule": 10.0},
        "recover-payments": {"task": "gather.recover_payments", "schedule": 60.0},
    },
)


def send_email(payload, message_id):
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    message["Message-ID"] = f"<{message_id}@gather.local>"
    message.set_content(payload["text"])
    if payload.get("html"):
        message.add_alternative(payload["html"], subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def process_message(db, message):
    if message.kind in {"publication_fanout", "engagement"}:
        from app.engagement import engagement_payload, publication_batch

        if message.kind == "publication_fanout":
            return await publication_batch(db, message)
        payload = await engagement_payload(db, message)
        if payload is None:
            message.skipped_at = datetime.now(UTC)
        else:
            await run_in_threadpool(send_email, payload, message.id)
    elif message.kind == "email":
        if message.topic == "registration" and message.booking_id:
            from app.bookings import lock_event
            from app.models import Booking

            booking = await db.scalar(sa.select(Booking).where(Booking.id == message.booking_id))
            event, host = await lock_event(db, booking.event_id)
            booking = await db.scalar(
                sa.select(Booking)
                .where(Booking.id == message.booking_id)
                .execution_options(populate_existing=True)
            )
            if (
                booking.status != "confirmed"
                or event.status != "published"
                or host.status != "approved"
            ):
                message.skipped_at = datetime.now(UTC)
                return
        await run_in_threadpool(send_email, message.payload, message.id)
    elif message.kind == "reminder":
        payload = await reminder_payload(db, message)
        if payload is None:
            message.skipped_at = datetime.now(UTC)
        else:
            await run_in_threadpool(send_email, payload, message.id)
    elif message.kind == "refund":
        await process_refund(db, UUID(message.payload["refund_id"]))
    elif message.kind == "delete_image":
        await run_in_threadpool(delete_object, message.payload["key"])
    elif message.kind == "image":
        row = await db.scalar(
            sa.select(EventMedia)
            .where(EventMedia.id == UUID(message.payload["media_id"]))
            .with_for_update()
        )
        if row is None or row.status == "ready":
            return
        content = await run_in_threadpool(get_object, row.source_key)
        try:
            image, thumb = await run_in_threadpool(image_variants, content)
        except ValueError as exc:
            row.status, row.error = "failed", str(exc)
            return
        prefix = f"public/{row.organizer_id}/{row.event_id}/{row.id}"
        image_key, thumbnail_key = f"{prefix}.webp", f"{prefix}-thumb.webp"
        await run_in_threadpool(put_object, image_key, image, "image/webp")
        await run_in_threadpool(put_object, thumbnail_key, thumb, "image/webp")
        row.image_key, row.thumbnail_key = image_key, thumbnail_key
        row.status, row.error = "ready", None
        # Originals expire via bucket lifecycle. Deleting before the DB commit
        # would make a worker crash between upload and commit impossible to retry.
    else:
        raise ValueError("Unknown outbox message kind")


async def deliver_message(db, message):
    message.attempts += 1
    try:
        if await process_message(db, message) is False:
            return
    except Exception as exc:
        # Keep only an allowlisted class label; exception strings can expose credentials.
        name = type(exc).__name__
        message.last_error_code = (
            name
            if name
            in {
                "SMTPAuthenticationError",
                "SMTPRecipientsRefused",
                "SMTPConnectError",
                "SMTPServerDisconnected",
                "TimeoutError",
                "ConnectionError",
                "RefundPending",
            }
            else "delivery_error"
        )
        if message.kind in {"email", "reminder", "engagement"} and message.attempts >= 8:
            message.failed_at = datetime.now(UTC)
        else:
            message.available_at = datetime.now(UTC) + timedelta(
                seconds=min(3600, 10 * 2 ** min(message.attempts, 8))
            )
    else:
        if not message.skipped_at:
            message.delivered_at = datetime.now(UTC)
        message.payload = {}
        message.last_error_code = None


async def drain():
    # Each Celery invocation owns its engine/event loop. No pooled connections leak across loops.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        from app.engagement import recover_waitlists

        await recover_waitlists(factory)
        await recover_refunds(factory)
        await recover_reminders(factory)
        async with factory() as db, db.begin():
            await expire_reservations(db)
        for _ in range(20):
            async with factory() as db, db.begin():
                # Workspace -> outbox ordering matches owner retry actions. SMTP and
                # reminder eligibility checks serialize with cancellation as well.
                due = sa.select(Outbox).where(
                    Outbox.delivered_at.is_(None),
                    Outbox.failed_at.is_(None),
                    Outbox.skipped_at.is_(None),
                    Outbox.available_at <= datetime.now(UTC),
                )
                candidate = await db.scalar(due.order_by(Outbox.available_at, Outbox.id).limit(1))
                if candidate is None:
                    break
                if candidate.organizer_id:
                    await db.scalar(
                        sa.select(Organizer)
                        .where(Organizer.id == candidate.organizer_id)
                        .with_for_update()
                    )
                message = await db.scalar(
                    due.where(Outbox.id == candidate.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if message is not None:
                    await deliver_message(db, message)
    finally:
        await engine.dispose()


@celery_app.task(name="gather.drain_outbox")
def drain_outbox():
    asyncio.run(drain())


async def recover_payment_batch():
    from app.payment_recovery import recover_payments

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        await recover_payments(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@celery_app.task(name="gather.recover_payments")
def recover_payment_task():
    asyncio.run(recover_payment_batch())
