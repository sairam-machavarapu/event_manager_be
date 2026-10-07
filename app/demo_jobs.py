"""Optional single-instance job polling for a sleeping free demo service."""

import asyncio
import logging
import time

from app.jobs import drain, recover_payment_batch

logger = logging.getLogger(__name__)


async def run_demo_jobs():
    next_payment_check = 0.0
    while True:
        try:
            await drain()
        except Exception:
            # Exception text can include connection strings or recipient data.
            logger.warning("Demo job batch failed; retrying on the next poll")
        if time.monotonic() >= next_payment_check:
            try:
                await recover_payment_batch()
            except Exception:
                logger.warning("Demo payment recovery failed; retrying on the next poll")
            next_payment_check = time.monotonic() + 60
        await asyncio.sleep(10)
