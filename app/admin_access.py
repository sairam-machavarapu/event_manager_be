"""Operator-only CLI for granting or revoking platform administrator access."""

import argparse
import asyncio

import sqlalchemy as sa

from app.database import engine, session_factory
from app.models import User


async def change(email, revoke):
    try:
        async with session_factory() as db, db.begin():
            user = await db.scalar(sa.select(User).where(User.email == email.strip().lower()))
            if user is None:
                raise SystemExit("Account not found. Register the account first.")
            if not revoke and user.verified_at is None:
                raise SystemExit(
                    "Verify this account's email before granting administrator access."
                )
            user.is_admin = not revoke
        print("Administrator access revoked." if revoke else "Administrator access granted.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    asyncio.run(change(args.email, args.revoke))
