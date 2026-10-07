# Gather demo release checklist

## Current deployment

- Frontend: https://event-manager-fe-zeta.vercel.app
- Backend: https://gather-api-a9nb.onrender.com
- Database and Redis: Render, same region as the API.
- Email: Brevo HTTPS API; sender verification remains an account setup step.
- Jobs: single API instance with `DEMO_JOBS_ENABLED=true`.
- Payments: sandbox only, disabled until configured and validated.
- Image storage: not configured; uploads need hosted S3-compatible storage.

Configure secrets in Render, not Git. Required deployment settings include
`DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `EMAIL_PROVIDER=brevo`,
`BREVO_API_KEY`, `MAIL_FROM`, `AUTH_COOKIE_SECURE=true`, and
`AUTH_ORIGIN=https://event-manager-fe-zeta.vercel.app`.
Use `python start.py` as the Docker Command. It applies migrations before
starting the API on `PORT`; migration failure prevents server startup.
Run exactly one instance and no separate Celery worker/beat in demo mode.

## Release checks

1. Push code and check the repository's GitHub Actions results.
2. Confirm Render deploys the intended commit and logs successful migration/startup.
3. Check `/api/v1/health/ready` returns 200, including all dependency checks.
4. In GitHub Actions, manually run **Check demo deployment** to check both
   backend readiness and the frontend API proxy. This is not a scheduled monitor.
5. Register/sign in through Vercel, request verification, and check Brevo's
   transactional logs for acceptance/delivery. Verify the link uses the frontend.
6. Confirm workspace creation/approval and a free event registration/QR flow.
7. Check images only after storage is configured. Never make originals public.

The readiness endpoint does not verify schema currency, job-runner activity,
email delivery, bucket access or a complete booking workflow. No live release
checks are claimed solely from adding these workflows.

## Backups and restoration

Install PostgreSQL client tools compatible with your server. Use the database's
external connection details when running locally. Enter credentials into local
environment variables, not committed files or screenshots:

```powershell
$env:PGHOST = "your-external-database-host"
$env:PGPORT = "5432"
$env:PGUSER = "your-database-user"
$env:PGDATABASE = "your-database-name"
$env:PGSSLMODE = "require"
# Set PGPASSWORD privately in your terminal or use a protected pgpass file.
New-Item -ItemType Directory -Path backups -Force
pg_dump --format=custom --file=backups/gather.dump
pg_restore --list backups/gather.dump
```

Listing a dump verifies readability, not successful restoration. Test restoration
against a NEW disposable database: change `PGDATABASE` to its name and run
`pg_restore --exit-on-error --no-owner --no-privileges --dbname="$env:PGDATABASE" backups/gather.dump`.
Never run that restore against the live database. Point a separate API instance
at the restored database and check records and booking flows before discarding it.
Store encrypted backups outside the Render service and restrict access: they
contain personal data. Back up media separately. Backup and restore execution
remain pending; these instructions do not create backups.

## Rollback

Record the last working commit in both repositories before deploying. For an
application-only regression, redeploy the previous working commit in the provider
dashboard, provided it is compatible with the current database schema. Keep a
backup before schema changes. Do not blindly downgrade migrations: old code may
be incompatible and downgrades can lose data. Use a forward repair for schema
issues unless a separately tested rollback procedure exists.

## Logs and demo limits

Render startup should show `Demo job runner started`. Brevo logs distinguish
provider acceptance and HTTP rejection; application logs must not include API
keys, cookies, connection strings or raw mail payloads. Health-check failures
and GitHub workflow failures require attention; external alerting/error tracking
is not configured yet.

Render Free sleeps, delaying queued jobs until wake-up. Free PostgreSQL expires
after 30 days; record your database expiry date and export data beforehand.
Free Redis is disposable; durable outbox records stay in PostgreSQL, but cache
and broker contents are not a backup. This is a demo, not a production launch.

References: https://render.com/docs/free and https://render.com/docs/deploys.
