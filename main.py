import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import account_actions as account_actions
from app.admin import router as admin_router
from app.admission import router as admission_router
from app.auth import router as auth_router
from app.bookings import public_router as availability_router
from app.bookings import router as bookings_router
from app.calendar import router as calendar_router
from app.cashfree import router as payments_router
from app.config import get_settings
from app.database import engine
from app.discovery import router as discovery_router
from app.engagement import router as engagement_router
from app.events import router as events_router
from app.health import router as health_router
from app.media import router as media_router
from app.notifications import router as notifications_router
from app.refunds import router as refunds_router
from app.ticket_types import router as ticket_types_router
from app.workspaces import router as workspace_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_task = None
    if get_settings().demo_jobs_enabled:
        from app.demo_jobs import run_demo_jobs

        job_task = asyncio.create_task(run_demo_jobs(), name="gather-demo-jobs")
    try:
        yield
    finally:
        if job_task is not None:
            job_task.cancel()
            with suppress(asyncio.CancelledError):
                await job_task
        await engine.dispose()


app = FastAPI(title="Gather API", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(workspace_router)
app.include_router(events_router)
app.include_router(ticket_types_router)
app.include_router(media_router)
app.include_router(discovery_router)
app.include_router(bookings_router)
app.include_router(availability_router)
app.include_router(payments_router)
app.include_router(refunds_router)
app.include_router(notifications_router)
app.include_router(admission_router)
app.include_router(admin_router)
app.include_router(engagement_router)
app.include_router(calendar_router)


@app.middleware("http")
async def private_account_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(
        (
            "/api/v1/auth/",
            "/api/v1/workspaces",
            "/api/v1/bookings",
            "/api/v1/payments",
            "/api/v1/tickets",
            "/api/v1/admin",
            "/api/v1/engagement",
        )
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default errors include rejected inputs, potentially passwords.
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422, content={"detail": errors}, headers={"Cache-Control": "no-store"}
    )
