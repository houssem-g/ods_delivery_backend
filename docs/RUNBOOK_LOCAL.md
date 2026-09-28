# Runbook — local stack

Everything here is **local**: nothing talks to Base44, staging or production. The cloud
deployment (repository `ods-iac`, namespace `delivery`) comes later; the last section
lists what it will need.

Two repositories side by side:

| Path | What |
|---|---|
| `ods_delivery_backend/` (this repo) | API, compose stack, DB tooling, Base44 import |
| `ods-delivery-own-backend/` | front, worktree of `ods-delivery` on branch **`feat/own-backend`** (never `main`: Base44 publishes `main`) |

## 1. Ports

| Port | Service | Started by |
|---|---|---|
| 5451 | PostgreSQL 17 + PostGIS (`db`), user `ods_delivery`, db `ods_delivery` | `make deps` / `make up` / `make up-full` |
| 9110 / 9111 | MinIO S3 API / console (`odsdlv-local` / `odsdlv-local-secret`) | same |
| 8125 / 1125 | Mailpit UI / SMTP (every e-mail: codes, resets) | same |
| 8110 | API, dev (reload): container `api` (`make up`) **or** host process (`make run`) | one of the two |
| 5190 | front dev server (`npm run dev:ods` in the front worktree) | by hand |
| 8111 | API, production-like (`api-prod`: image as built, no reload, JSON logs) | `make up-full` |
| 5191 | front image (`Dockerfile.ods`, nginx) talking to 8111 | `make up-full` |

Every host port can be moved with a variable (`DB_PORT`, `MINIO_PORT`, `MINIO_CONSOLE_PORT`,
`MAILPIT_UI_PORT`, `MAILPIT_SMTP_PORT`, `API_PORT`, `API_FULL_PORT`, `FRONT_PORT`), in `.env`
or on the command line. Ports avoid the ODS main stack (8000, 5441, 1337, 3000/3001).

## 2. First run

```bash
cp .env.example .env          # placeholders only; nothing secret is needed locally
make deps                     # db, minio (+ bucket), mailpit
uv sync                       # virtualenv with the dev tools
make migrate                  # alembic upgrade head
make seed                     # local admin, Playwright QA accounts, default settings
make run                      # API with reload on http://localhost:8110
# front worktree:
cd ../ods-delivery-own-backend && npm ci && npm run dev:ods     # http://localhost:5190
```

## 3. Everyday commands (`make help` lists them all)

| Command | What |
|---|---|
| `make deps` | db + minio + mailpit only (API on the host with `make run`) |
| `make up` | dev stack in docker: + `api` with reload on 8110 (code mounted from `./app`) |
| `make up-full` | production-like stack: + `api-prod` on 8111 + `front` image on 5191 |
| `make down` | stop everything, full profile included (volumes kept) |
| `make logs` / `make logs-full` | follow `api` / `api-prod` + `front` |
| `make run` | API on the host, reload, port 8110 |
| `make migrate` / `make revision m="…"` / `make check-heads` | Alembic |
| `make seed` | idempotent local seed |
| `make test` | pytest (database `ods_delivery_test`, override `TEST_DATABASE_URL=…`) |
| `make lint` / `make fmt` | ruff |
| `make precommit` | pre-commit hooks on every file (ruff, end-of-file, private keys, gitleaks) |
| `make audit` | pip-audit of `uv.lock` (`AUDIT_ARGS="--ignore-vuln ID"`) |
| `make shell-db` | psql on `ods_delivery` |
| `make backup` / `restore` / `db-counts` / `reset-local` / `import-local` | §5, §6 |

`make up` and `make run` both use port 8110: use one or the other. `make up-full` does not
start the dev `api`, so it coexists with `make run` + `npm run dev:ods` (5190 → 8110 and
5191 → 8111 against the same database; realtime and the job leader lock work across both).

The git hook is opt-in: `uv run pre-commit install` (hooks are shared by every worktree of
the repository).

### The full profile

`make up-full` builds two images:
- `odsdlv-api:local` from `./Dockerfile` (the same image as the dev `api`);
- `odsdlv-front:local` from `$FRONT_CONTEXT/Dockerfile.ods`, `FRONT_CONTEXT` defaulting to
  `../ods-delivery-own-backend`. Build args: `VITE_API_URL=http://localhost:8111`,
  `FILES_ORIGIN=http://localhost:9110` (both end up in the bundle and in the CSP).
  Details of the image (cache rules, CSP, headers): front `docs/OWN_BACKEND_CLIENT.md` §9.

`api`, `api-prod` and `front` run with a read-only root filesystem (tmpfs on `/tmp`, and
`/var/cache/nginx` for the front), no Linux capabilities, `no-new-privileges`, as non-root
users (uid 10001 `app`, uid 101 `nginx`). Every long-running service has a healthcheck
(API: `/api/health`; front: `/healthz`; Mailpit: its image's own check).

Rebuild after a front change: `make up-full` again (it always passes `--build`). Avoid
`docker compose up --wait` with this file: the one-shot `minio-init` exits, which `--wait`
reports as a failure.

## 4. Health, logs, metrics

- `GET /api/health` (liveness), `GET /api/health/ready` (db + storage, 503 when one is down).
- Logs: `LOG_FORMAT=text` (default) or `json` (`api-prod` uses json). One access line per
  request: method, **route template** (`/api/entities/{name}/{doc_id}`, never the raw path or
  query string), status, duration, `user` = keyed hash of the user id. Tokens, e-mails, phone
  numbers, passwords and codes are redacted from every line. uvicorn's own access log is off
  (it would print the WebSocket token).
- Request id: `X-Request-ID` is accepted when it looks safe (8-128 chars of `[A-Za-z0-9._:-]`),
  otherwise generated; echoed on every response, exposed to the browser (CORS), present in
  every log line of the request and in Sentry events.
- Metrics: `GET /api/metrics` (Prometheus) answers 404 until `METRICS_TOKEN` is set; then
  send it as `X-Metrics-Token: …` (or `Authorization: Bearer …`):
  ```bash
  curl -H "X-Metrics-Token: $METRICS_TOKEN" http://localhost:8110/api/metrics
  ```
  `odsd_http_requests_total{method,route,status}`, `odsd_http_request_duration_seconds`,
  `odsd_ws_connections`, `odsd_job_runs_total{job}`, `odsd_job_failures_total{job}`,
  `odsd_job_duration_seconds`, `odsd_scheduler_leader`, `odsd_db_pool_{size,checked_out,checked_in,overflow}`,
  plus process metrics. One registry per process: keep `WEB_CONCURRENCY=1`.
- Sentry: off while `SENTRY_DSN` is empty. When set: no default PII, no request bodies,
  sensitive headers and cookies dropped, strings redacted, user = hash.

## 5. Database operations

All of them go through the compose `db` service (`docker compose exec db …`), so no local
PostgreSQL client is needed. Another compose project: `COMPOSE="docker compose -p name"`
and `DB_PORT=…`.

| Command | What |
|---|---|
| `make backup [DB=ods_delivery]` | `pg_dump --format=custom` → `backups/<db>_<UTC timestamp>.dump` (dir mode 700, file 600, git-ignored: **real data**), checked with `pg_restore --list` |
| `make restore FILE=backups/….dump DB=<new name>` | creates `<new name>` and restores into it (`--no-owner`, extensions come from the dump), then prints the row counts. Refuses an existing database, and always `ods_delivery`, unless `FORCE=1` (drops it first, cutting connections) |
| `make db-counts DB=<name>` | exact row count per table + total (compare a restore with its source) |
| `make reset-local [DB=ods_delivery] [YES=1]` | **drops** the database, recreates it with the extensions (citext, postgis, pg_trgm, pgcrypto), `alembic upgrade head`, seed. Asks to type the database name unless `YES=1`; refuses the pytest databases |

Restore drill (done on 2026-09-28, identical counts, 16 501 rows):

```bash
make backup
make restore FILE=backups/ods_delivery_<ts>.dump DB=ods_delivery_restore_check
make db-counts DB=ods_delivery        # compare with the counts printed by restore
docker compose exec db psql -U ods_delivery -d postgres -c 'DROP DATABASE ods_delivery_restore_check'
```

After a `reset-local` or a `restore FORCE=1` of the database a running API uses, the API
reconnects by itself (the job leader retries every 30 s); restart it to be sure.

## 6. Importing the Base44 data

The export is taken beforehand (docs/MIGRATION.md §1; `migrate/export.mjs`) into
`~/ODS-backups/migration/<YYYY-MM-DD>/` (mode 600, never in git).

```bash
make reset-local                    # optional: start from an empty, migrated, seeded database
make import-local                   # latest export dir, --files, into ods_delivery
make import-local ARGS="--dry-run"  # transform + import in a rolled-back transaction
make import-local DB=other EXPORT_DIR=~/ODS-backups/migration/2026-09-28 ARGS=""
```

`import-local` picks the directory with the latest name under `EXPORT_ROOT`
(default `~/ODS-backups/migration`) that contains entity JSON files, and runs
`python -m migrate.pipeline` (transform → idempotent import → verify → report). Default
`ARGS="--files"`: the exported files are uploaded to the bucket of `S3_ENDPOINT_URL`
(the local MinIO). Exit code ≠ 0 when verify is red; the report is written to
`<export dir>/_report.md`. A rerun on the same export changes nothing. The general form
(any database URL) stays `make import-base44 EXPORT_DIR=… DATABASE_URL=…`.

## 7. Playwright suites of the front against the local stack

In the front worktree (`../ods-delivery-own-backend`), with the backend seeded (the QA
accounts come from `tests/helpers/constants.ts`):

```bash
# client unit tests + ods Welcome smoke (mocked API, no backend needed)
npm run test:ods-client

# dev server (start it first: the default config would otherwise start the Base44 dev server)
npm run dev:ods &                                   # 5190 -> API 8110 (make run or make up)
BASE_URL=http://127.0.0.1:5190 npx playwright test tests/auth.spec.ts --project=chromium

# production-like image
make up-full                                        # (backend repo) 5191 -> API 8111
BASE_URL=http://127.0.0.1:5191 npx playwright test tests/customer.spec.ts --project=chromium
```

`playwright.config.ts` reuses a server already listening on `BASE_URL` (outside CI); it
never has to start one when you run against 5190 or 5191. Suites that only make sense on
Base44 (rate budget, RLS JSON evaluation, published-app audits) are expected to fail here.
Use `localhost` rather than `127.0.0.1` when you sign in by hand: the refresh cookie is set
for the API host, and `localhost` / `127.0.0.1` are different sites for the browser.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `port is already allocated` / `address already in use` | `ss -ltnp 'sport = :8110'` to find the owner; stop it, or move the port (`API_PORT=8120 make up`, `API_FULL_PORT`, `FRONT_PORT`…). `make up` and `make run` both want 8110 |
| front on 5191 cannot reach the API | `api-prod` must be healthy (`docker compose ps`); the image calls `http://localhost:8111` (baked in at build time: rebuild after changing `API_FULL_PORT`) |
| CORS error in the browser | the origin must be in `CORS_ORIGINS` (defaults: 5190 and 5191, `localhost` and `127.0.0.1`); `api-prod` gets `FRONT_PORT` automatically |
| CSP violation in the browser console (image only) | a new external origin in the front: add it to `docker/ods/gen-headers.mjs` in the front worktree, or `--build-arg CSP_EXTRA_CONNECT/CSP_EXTRA_IMG` |
| files owned by root in the repos (`.venv`, `__pycache__`, `node_modules`, `dist-ods`) | `docker run --rm -v "$PWD:/w" alpine chown -R "$(id -u):$(id -g)" /w` in that repo |
| db killed / restarting (OOM) | `db` has `mem_limit: 768m` (384m was reached with the imported data plus several test databases); `docker stats` to check. Drop leftover databases (`ods_delivery_*_check`, old test DBs) |
| machine short of RAM | build one image at a time (`docker compose build api-prod`, then `front`); the front build caps Node at 1.5 GB (`NODE_OPTIONS`); `make deps` + `make run` is the lightest setup; `docker builder prune` reclaims the build cache |
| `make restore` refuses | the target exists (new name, or `FORCE=1`); `ods_delivery` always needs `FORCE=1` |
| `reset-local` says "not a terminal" | run it in a terminal, or `YES=1` |
| tests fail with "tests only run against a database named ods_delivery_test" | `TEST_DATABASE_URL` must point to a database whose name contains `ods_delivery_test` |
| `make audit` fails on `ecdsa` (PYSEC-2026-1325, no fix) | transitive dependency of `python-jose`; tokens are HS256, the ECDSA code is never used. Track it; replacing `python-jose` by `PyJWT` removes it |

## 9. Cloud later (what `ods-iac` will need)

Not done here. Checklist for the namespace `delivery`:

**Images** (build in CI, push to the registry, pin by digest):
- API: `./Dockerfile` target `runtime`, `--build-arg APP_RELEASE=<git sha>`; non-root uid
  10001, read-only root filesystem + `emptyDir` on `/tmp`, port 8000, `/api/health`
  (liveness) and `/api/health/ready` (readiness).
- Front: `Dockerfile.ods` of `ods-delivery` **branch `feat/own-backend`**, build args
  `VITE_API_URL=https://<api domain>`, `FILES_ORIGIN=https://<files origin>`,
  `VITE_FIREBASE_*` (public), `APP_VERSION`; non-root uid 101, read-only + `emptyDir` on
  `/tmp` and `/var/cache/nginx`, port 8080, `/healthz`. One image per API origin.
  Workflow template: front `docs/ci/ods-image.yml`.

**Migrations**: `alembic upgrade head` as a Job (or init container of one pod) before the
rollout, not in every replica's start command.

**Configuration (non-secret)**: `ENVIRONMENT=production`, `LOG_FORMAT=json`, `LOG_LEVEL`,
`CORS_ORIGINS`, `PUBLIC_APP_URL`, `API_PUBLIC_URL`, `GOOGLE_REDIRECT_URI`, `COOKIE_SECURE=true`,
`COOKIE_DOMAIN`, `DB_SSLMODE=require`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_MAX_CONNECTIONS`,
`DB_CONNECTION_RESERVE`, `WEB_CONCURRENCY=1`, `DEPLOYMENT_REPLICAS` (= `spec.replicas`),
`S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL`, `S3_PUBLIC_BASE_URL` (CDN), `S3_REGION`,
`S3_BUCKET`, `EMAIL_PROVIDER`, `EMAIL_FROM`, `SMTP_HOST/PORT/STARTTLS/TLS`, `PUSH_PROVIDER`,
`OSM_USER_AGENT` (real contact), `NOMINATIM_*`, `OVERPASS_*`, `OSM_REFRESH_ENABLED`, `OSRM_URL`,
`QA_ACCOUNTS`, `SCHEDULER_ENABLED`, `REALTIME_ENABLED`, `RATE_LIMIT_*`, `SENTRY_ENVIRONMENT`,
`SENTRY_TRACES_SAMPLE_RATE`, `APP_RELEASE`. The full list with defaults is `.env.example`
(a test checks it names every setting).

**Secrets** (Kubernetes Secrets / sealed, never in git): `JWT_SECRET` (≥ 32 chars, refused
otherwise outside local), `DATABASE_URL` (password), `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
`SMTP_USERNAME`, `SMTP_PASSWORD`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, the Firebase
service-account JSON (mounted file, path in `FIREBASE_CREDENTIALS_PATH`), `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WINSMS_API_KEY`,
`WINSMS_SENDER`, `CRON_SECRET`, `METRICS_TOKEN`, `SENTRY_DSN`.

**Database**: managed PostgreSQL 17 with the extensions `citext`, `postgis`, `pg_trgm`,
`pgcrypto` created by an admin once (the app role is not superuser). The realtime `LISTEN`
connection and the job leader's advisory lock need **session** semantics: connect directly
(or through a session-mode pool), never through a transaction-mode PgBouncer. Connection
budget: `(DB_MAX_CONNECTIONS - DB_CONNECTION_RESERVE) / (WEB_CONCURRENCY × DEPLOYMENT_REPLICAS)`
per process, plus 2 per process (LISTEN, leader lock) inside the reserve. Backups: the
provider's PITR plus a periodic `pg_dump --format=custom` like `make backup`.

**Object storage**: a DO Spaces bucket (private) with the `public/` prefix anonymously
readable (bucket policy, or a CDN in front with `S3_PUBLIC_BASE_URL`); presigned GETs are
signed for `S3_PUBLIC_ENDPOINT_URL`. The files origin goes into the front's CSP
(`FILES_ORIGIN`). The Base44 files are uploaded by `make import-base44 … ARGS="--files"`
pointed at the Spaces endpoint during the cutover.

**Domains and TLS**: one host for the front and one for the API (or one host with `/api`
routed to the API: then `VITE_API_URL` is that origin). TLS at Traefik / the load
balancer; HSTS set there (the images serve plain HTTP). `COOKIE_SECURE=true`; the refresh
cookie's `SameSite` must allow the front origin → API origin calls (same site = simplest).

**Jobs**: no CronJob needed. APScheduler runs in every API process and only the holder of
`pg_try_advisory_lock(SCHEDULER_LOCK_KEY)` runs the jobs; another pod takes over within
`SCHEDULER_LEADER_RETRY_SECONDS` when it dies. `odsd_scheduler_leader` tells which pod leads.
Manual runs: `POST /api/admin/jobs/{name}/run` with `x-cron-token: $CRON_SECRET`.

**WebSockets through Traefik**: `/api/ws` upgrades natively (IngressRoute to the API
Service; no special middleware). Keep idle timeouts above the client's 25 s ping. **No
sticky sessions**: every pod LISTENs on `pg_notify('delivery_events')` and fans the events
out to its own sockets, so a client can land on any pod. During a rolling update clients
reconnect with backoff and resync.

**Observability**: `LOG_FORMAT=json` → Loki (fields `request_id`, `route`, `status`,
`duration_ms`, `user`); Prometheus scrape of `/api/metrics` on every pod with the
`X-Metrics-Token` header; Sentry optional. The front's nginx logs JSON without query strings.

**Network egress** from the API: SMTP, Firebase (FCM), Meta Graph API, WinSMS, Nominatim,
Overpass, OSRM, Google OAuth, the S3 endpoint.
