"""Single-use account links; mail is committed together with the token."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.auth import (
    COOKIE,
    DB,
    Account,
    Credentials,
    Registration,
    account,
    attempts,
    current_user,
    hash_password,
    mutations,
    router,
    token_hash,
)
from app.config import get_settings
from app.models import AccountToken, AuthSession, Outbox, User
from app.notifications import email_payload


class EmailInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return Credentials.normalize_email(value)


class TokenInput(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class ResetInput(TokenInput):
    password: str = Field(min_length=12, max_length=128)


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        return Registration.clean_name(value)


async def queue_link(db, user, purpose):
    # Serialize issuance with redemption and recovery on the account row.
    await db.scalar(sa.select(User).where(User.id == user.id).with_for_update())
    await db.execute(
        sa.delete(AccountToken).where(
            AccountToken.user_id == user.id, AccountToken.purpose == purpose
        )
    )
    token = secrets.token_urlsafe(32)
    hours = 24 if purpose == "verify" else 1
    db.add(
        AccountToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=token_hash(token),
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
        )
    )
    page = "verify-email" if purpose == "verify" else "reset-password"
    url = f"{get_settings().auth_origin}/{page}#token={token}"
    db.add(
        Outbox(
            kind="email",
            topic=purpose,
            payload=email_payload(
                user.email,
                "Verify your Gather email" if purpose == "verify" else "Reset your Gather password",
                f"Expires in {hours} hour(s). If you did not request this, ignore this email.",
                link=url,
                label="Continue to your account",
            ),
        )
    )


@router.patch("/me", response_model=Account, dependencies=mutations)
async def update_profile(body: ProfileInput, db: DB, user: Annotated[User, Depends(current_user)]):
    user.display_name = body.display_name
    await db.commit()
    return account(user)


@router.post("/verification/request", status_code=202, dependencies=attempts)
async def request_verification(db: DB, user: Annotated[User, Depends(current_user)]):
    if user.verified_at is None:
        await queue_link(db, user, "verify")
        await db.commit()
    return {"message": "Check your inbox for a verification link."}


@router.post("/recovery/request", status_code=202, dependencies=attempts)
async def request_recovery(body: EmailInput, db: DB):
    user = await db.scalar(sa.select(User).where(User.email == body.email.strip().lower()))
    if user is not None:
        await queue_link(db, user, "recover")
        await db.commit()
    return {"message": "If an account exists for that email, a reset link will arrive shortly."}


async def redeem(db, raw, purpose):
    digest = token_hash(raw)
    link = await db.scalar(
        sa.select(AccountToken).where(
            AccountToken.token_hash == digest, AccountToken.purpose == purpose
        )
    )
    if link is None:
        raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
    user = await db.scalar(sa.select(User).where(User.id == link.user_id).with_for_update())
    # Conditional DELETE RETURNING makes redemption atomic even across concurrent requests.
    result = await db.execute(
        sa.delete(AccountToken)
        .where(
            AccountToken.token_hash == digest,
            AccountToken.purpose == purpose,
            AccountToken.expires_at > datetime.now(UTC),
        )
        .returning(AccountToken.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
    return user


@router.post("/verification/confirm", response_model=Account, dependencies=attempts)
async def confirm_verification(body: TokenInput, db: DB):
    user = await redeem(db, body.token, "verify")
    user.verified_at = datetime.now(UTC)
    await db.commit()
    return account(user)


@router.post("/recovery/confirm", dependencies=attempts)
async def confirm_recovery(body: ResetInput, db: DB, response: Response):
    password = await run_in_threadpool(hash_password, body.password)
    user = await redeem(db, body.token, "recover")
    user.password_hash = password
    await db.execute(sa.delete(AuthSession).where(AuthSession.user_id == user.id))
    await db.execute(sa.delete(AccountToken).where(AccountToken.user_id == user.id))
    await db.commit()
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="lax", secure=get_settings().auth_cookie_secure
    )
    return {"message": "Password changed. Sign in with your new password."}
