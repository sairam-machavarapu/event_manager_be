# Gather Backend

Gather is an event discovery and management platform. This repository contains
its FastAPI API, database migrations and background jobs. The Next.js frontend
runs separately.

## Technology

Python 3.14, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Redis and Celery.
S3-compatible storage handles event images; SMTP delivers account and event mail.
Cashfree integration currently supports sandbox payments only.

## Features

- Session authentication, email verification and account recovery.
- Organiser membership, approval and event publishing workflows.
- Event discovery, ticket inventory and reservation expiry.
- Sandbox payments, authoritative payment checks and refund recovery.
- QR ticket issuance and atomic admission checks.
- Saved events, follows, waitlists and scheduled notifications.
- Administrative moderation, audit records and webhook receipts.

## Prerequisites

Install Python 3.14. Provision PostgreSQL, a Redis cache and a separate Redis
Celery broker. For email and media functionality also provide SMTP and an
S3-compatible bucket. The database and bucket must exist before use.
Local defaults in `app/config.py` do not create these services.

## Run locally

From this repository's root on Windows:

```powershell
py -3.14 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Set environment variables from the configuration table before running migrations.
For local overrides, create an ignored `.env` yourself. On macOS/Linux use
`python3.14 -m venv .venv` and `.venv/bin/python`.

API documentation: http://localhost:8000/docs.
Start the frontend separately on http://localhost:3000.

Run the worker and scheduler in two additional terminals from this root:

```powershell
./.venv/Scripts/python.exe -m celery -A app.jobs:celery_app worker --loglevel=info --pool=solo
```

```powershell
./.venv/Scripts/python.exe -m celery -A app.jobs:celery_app beat --loglevel=info
```

The solo worker is for Windows development. Linux deployments use the default
worker pool. Run exactly one scheduler. Workers deliver email, process images
and handle refunds; the scheduler runs recovery, reminders and expiry jobs.

## Configuration

| Variables | Purpose |
| --- | --- |
| DATABASE_URL | Async SQLAlchemy PostgreSQL URL beginning `postgresql+asyncpg://` |
| REDIS_URL | Redis cache URL |
| CELERY_BROKER_URL | Separate Redis job broker URL |
| AUTH_ORIGIN | Exact frontend origin, without a trailing slash |
| AUTH_COOKIE_SECURE | `false` locally; `true` over production HTTPS |
| SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD | SMTP connection and credentials |
| SMTP_STARTTLS, MAIL_FROM | STARTTLS setting and verified sender |
| STORAGE_ENDPOINT, STORAGE_REGION, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY | S3-compatible connection |
| STORAGE_BUCKET, MEDIA_PUBLIC_URL | Bucket name and public derivative URL |
| CASHFREE_ENABLED, CASHFREE_CLIENT_ID, CASHFREE_CLIENT_SECRET | Optional sandbox activation |
| CASHFREE_API_VERSION, CASHFREE_NOTIFY_URL | Sandbox API version and public webhook URL |

SMTP supports STARTTLS, typically port 587, rather than implicit TLS on port 465.
Keep image originals private and expose only `public/` derivatives. Configure
bucket retention for originals. Local Mailpit does not send to real inboxes.

## Checks

```powershell
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m ruff format --check .
```


## Project layout

- `main.py`: API entry point and routes.
- `app/`: domain models, API services and background jobs.
- `migrations/`: versioned database schema changes.
- `Dockerfile`: deployable Python application image.

## Deploy to Render

Import this backend repository with Docker runtime, repository root as the Root
Directory and `./Dockerfile` as the Dockerfile. Create PostgreSQL and separate
cache/broker Key Value services in the same region. Share backend environment
settings across these services:

| Service | Type | Docker command |
| --- | --- | --- |
| API | Web service | `python start.py` |
| Worker | Background worker | `celery -A app.jobs:celery_app worker --loglevel=info --concurrency=2` |
| Scheduler | Background worker, one instance | `celery -A app.jobs:celery_app beat --loglevel=info` |

Use `/api/v1/health/ready` as the API health-check path. For the single-instance
free demo, `python start.py` applies migrations before starting the API and uses
Render's `PORT`. For deployments with multiple API instances, use a dedicated
migration step and start Uvicorn separately. Run `alembic upgrade head`
as the API pre-deploy command where supported, or through a deployment job/shell
before starting the new release. Replace the Render PostgreSQL URL scheme with
`postgresql+asyncpg://`. Use internal database and Redis URLs, and configure the
broker with no eviction.

Set `AUTH_ORIGIN` to the Vercel production origin and `AUTH_COOKIE_SECURE=true`.
Configure hosted SMTP and storage credentials through Render environment settings.
Keep `CASHFREE_ENABLED=false` until sandbox validation. Production payment
collection, marketplace onboarding, ledger and payout work remain outstanding.
Existing IP-based authentication limits can be shared by proxied frontend traffic;
production proxy handling remains part of the hardening work.

## Environment files

Configure deployed settings directly in Render → Environment. No backend
`.env.example` is included. An optional local `.env` remains ignored by Git.
Never commit database, email, storage or payment credentials.

## Brevo email on Render Free

Add these variables in Render's backend environment settings:

```text
EMAIL_PROVIDER=brevo
BREVO_API_KEY=<your private Brevo API key>
MAIL_FROM=Gather <your-verified-sender@example.com>
```

Use an API key, not an SMTP key. Verify the sender in Brevo first. No SMTP
settings are needed in Brevo mode. Local development defaults to SMTP/Mailpit.
The existing outbox retries failed requests; provider acceptance does not confirm
inbox delivery, and ambiguous network failures can cause duplicate mail.

Email still requires the background job runner. This integration does not by
itself provide free worker/scheduler hosting; the free-demo job arrangement and
image storage still need to be configured. No real email has been sent during
setup. Keep the API key out of Git and the frontend environment.
