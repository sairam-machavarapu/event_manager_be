"""Bounded recovery of missing sandbox payment notifications."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from fastapi import HTTPException

from app import cashfree
from app.models import PaymentAttempt


async def recover_payments(factory):
    settings = cashfree.get_settings()
    if not (
        settings.cashfree_enabled
        and settings.cashfree_client_id
        and settings.cashfree_client_secret
    ):
        return
    for _ in range(5):
        async with factory() as db:
            now = datetime.now(UTC)
            attempt = await db.scalar(
                sa.select(PaymentAttempt)
                .where(
                    PaymentAttempt.provider == "cashfree",
                    PaymentAttempt.status == "pending",
                    PaymentAttempt.provider_reference.is_not(None),
                    PaymentAttempt.next_check_at <= now,
                )
                .order_by(PaymentAttempt.next_check_at, PaymentAttempt.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if attempt is None:
                return
            attempt_id, reference = attempt.id, attempt.provider_reference
            # Lease before network I/O, without reversing the fulfilment lock order.
            attempt.next_check_at = now + timedelta(minutes=5)
            await db.commit()
            try:
                order = await cashfree.provider_request(
                    "GET", f"/orders/{reference}", missing_ok=True
                )
                if order is not None:
                    await cashfree.reconcile(db, attempt_id, order)
            except HTTPException:
                await db.rollback()
                # Retry on the next lease; never log provider bodies or credentials.
