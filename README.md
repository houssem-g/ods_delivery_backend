# ODS Delivery API

Backend of the ODS Delivery app (FastAPI, PostgreSQL 17 + PostGIS, SQLAlchemy 2 async,
Alembic). Design: `docs/ARCHITECTURE.md`. Field mapping of the legacy entities:
`docs/FIELD_MAPPING.md`. How to add entities / functions / events /
jobs: `docs/COMPAT_GUIDE.md`.

## Quickstart (local only)

Requirements: Docker (compose v2), [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
cp .env.example .env         # placeholders; nothing secret is needed locally
make deps                    # db (5451), MinIO (9110/9111), Mailpit (8125/1125)
uv sync                      # virtualenv with dev tools
make migrate                 # alembic upgrade head
make seed                    # local admin, Playwright QA accounts, default settings
make run                     # API with reload on http://localhost:8110
```

Or everything in docker: `make up` (the api container migrates at start), then
`make seed` from the host (it reaches the db on 5451 and reads the QA constants file,
which is not mounted in the container).

| URL | What |
|---|---|
| http://localhost:8110/api/health, `/api/health/ready` | liveness, readiness (db + storage) |
| http://localhost:8110/api/docs | OpenAPI (local only) |
| http://localhost:8125 | Mailpit: every e-mail (sign-up codes, resets, account-setup codes) |
| http://localhost:9111 | MinIO console (`odsdlv-local` / `odsdlv-local-secret`) |

Seeded accounts: `LOCAL_ADMIN_EMAIL` (default `admin@ods.local`; without
`LOCAL_ADMIN_PASSWORD` it has no password: sign in once, take the code from Mailpit,
set one via account-setup) and the Playwright QA accounts, whose passwords are read
at run time from `../ods-delivery/tests/helpers/constants.ts` (or `TEST_*` env vars).

Production-like stack (API image without reload on 8111, front image on 5191):
`make up-full`. Every command, backups, reset, Playwright against the
local stack, troubleshooting and what the cloud will need: `docs/RUNBOOK_LOCAL.md`.

## Everyday commands

```bash
make test          # pytest on the ods_delivery_test database (+ coverage)
make lint          # ruff check + format check      (make fmt to fix)
make revision m="add foo"   # autogenerate a migration, then review it by hand
make check-heads   # exactly one alembic head
make shell-db      # psql
make down          # stop the stack (volumes kept)
```

Tests need `make deps` running; the file tests are skipped when MinIO is down.
Nothing here talks to staging or production.

## Layout

```
app/api        routers (auth, entities, functions, files, ws, admin, health)
app/api/functions  one module per function (file name = function name)
app/compat     legacy document shapes: registry, filter translator, entities
app/models     SQLAlchemy models (mirror the migrations; `alembic check` is clean)
app/migrations Alembic
app/security   passwords, tokens, e-mail codes, dependencies
app/services   domain services (auth, e-mail, push, phones, ...)
app/realtime   events (pg_notify on commit), listener, hub
app/jobs       leader-elected APScheduler + job registry
app/storage    S3 / MinIO client and key conventions
scripts        seed_local.py, check_single_head.py
```
