import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import get_session
from app.models import AuthSession, User

router = APIRouter(prefix="/api/v1/auth", tags=["accounts"])
DB = Annotated[AsyncSession, Depends(get_session)]
COOKIE = "gather_session"
SESSION_SECONDS = 60 * 60 * 24 * 7


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=131072, r=8, p=1, maxmem=256 * 1024 * 1024, dklen=32
    )
    return f"scrypt$131072$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, digest = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        candidate = hash_password(password, bytes.fromhex(salt)).split("$")[-1]
        return hmac.compare_digest(candidate, digest)
    except ValueError, TypeError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_same_origin(request: Request):
    # Non-browser API clients must also explicitly opt into JSON mutations.
    if request.headers.get("x-gather-request") != "1":
        raise HTTPException(403, "Missing request protection header")
    origin = request.headers.get("origin")
    if origin is not None and origin != get_settings().auth_origin:
        raise HTTPException(403, "Origin is not allowed")


async def rate_limit(request: Request):
    address = request.client.host if request.client else "unknown"
    key = "auth:attempts:" + token_hash(address)
    try:
        async with Redis.from_url(get_settings().redis_url) as redis:
            count = await redis.eval(
                "local n=redis.call('INCR',KEYS[1]); "
                "if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n",
                1,
                key,
            )
    except RedisError as exc:
        raise HTTPException(503, "Account access is temporarily unavailable") from exc
    if count > 20:
        raise HTTPException(
            429, "Too many attempts. Try again in five minutes.", headers={"Retry-After": "300"}
        )


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        local, separator, domain = value.partition("@")
        if (
            not separator
            or not local
            or "." not in domain
            or "@" in domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Enter a valid email address")
        return value


class Registration(Credentials):
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()


class Account(BaseModel):
    id: UUID
    email: str
    display_name: str
    verified: bool
    is_admin: bool


def account(user: User) -> Account:
    return Account(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        verified=user.verified_at is not None,
        is_admin=user.is_admin,
    )


async def start_session(user: User, db: AsyncSession, response: Response, request: Request):
    old = request.cookies.get(COOKIE)
    if old:
        await db.execute(sa.delete(AuthSession).where(AuthSession.token_hash == token_hash(old)))
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=token_hash(token),
            expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_SECONDS),
        )
    )
    await db.commit()
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=get_settings().auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


async def current_user(request: Request, db: DB) -> User:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "Sign in to continue")
    user = await db.scalar(
        sa.select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            AuthSession.token_hash == token_hash(token), AuthSession.expires_at > datetime.now(UTC)
        )
    )
    if user is None:
        raise HTTPException(401, "Sign in to continue")
    return user


mutations = [Depends(require_same_origin)]
attempts = [*mutations, Depends(rate_limit)]


@router.post("/register", response_model=Account, status_code=201, dependencies=attempts)
async def register(body: Registration, db: DB, response: Response, request: Request):
    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=await run_in_threadpool(hash_password, body.password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Unable to create account with those details") from exc
    from app.account_actions import queue_link

    await queue_link(db, user, "verify")
    await start_session(user, db, response, request)
    return account(user)


@router.post("/login", response_model=Account, dependencies=attempts)
async def login(body: Credentials, db: DB, response: Response, request: Request):
    # Recovery takes the same user lock, so an in-flight old-password login cannot
    # create a session after reset has revoked the account's sessions.
    user = await db.scalar(sa.select(User).where(User.email == body.email).with_for_update())
    # Perform the same expensive derivation when the email does not exist.
    encoded = user.password_hash if user else ("scrypt$131072$8$1$" + "00" * 16 + "$" + "00" * 32)
    valid = await run_in_threadpool(verify_password, body.password, encoded)
    if user is None or not valid:
        raise HTTPException(401, "Email or password is incorrect")
    await start_session(user, db, response, request)
    return account(user)


@router.get("/me", response_model=Account)
async def me(response: Response, user: Annotated[User, Depends(current_user)]):
    response.headers["Cache-Control"] = "no-store"
    return account(user)


@router.post("/logout", status_code=204, dependencies=mutations)
async def logout(request: Request, response: Response, db: DB):
    token = request.cookies.get(COOKIE)
    if token:
        await db.execute(sa.delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        await db.commit()
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="lax", secure=get_settings().auth_cookie_secure
    )
    response.headers["Cache-Control"] = "no-store"
