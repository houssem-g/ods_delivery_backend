# ODS Delivery — backend

Source of truth for the design. Written 2026-09-28 from the database audit
(`ods-delivery/docs/DB_AUDIT.md`, sections 4-6). Everything runs **locally**
first (docker compose); the cloud deployment (ods-iac, namespace `delivery`)
comes after.

## 1. Goals and non-goals

- Provide every service the app uses: database, row-level security,
  auth (password, e-mail OTP, reset, Google), realtime subscriptions, backend
  functions, scheduled jobs, file uploads (public and private), push (FCM),
  WhatsApp/SMS, OSM places/geocoding.
- **Stable client surface**: the app calls one client object
  (`src/api/odsClient.js`) with this surface:
  `entities.X.filter/list/get/create/update/delete/subscribe`,
  `functions.invoke(name, payload)`, `auth.*`, `integrations.Core.*`,
  `appLogs.logUserInApp`. The Playwright suites run against the local stack.
- **The database is normalized** (audit §6.2): real FKs, uuid keys, numeric
  money, PostGIS, derived counters, append-only status events. The legacy
  document shape exists only at the API edge (`app/compat/`).
- **No generic write path**: an entity write coming from the front is accepted
  only through an explicit per-entity policy (whitelisted fields, owner checks,
  status transition matrix) that calls the same domain services as the
  functions. A courier never writes status or amounts directly.
- Non-goals now: iOS native.

## 2. Stack

| Concern | Choice |
|---|---|
| Language / web | Python 3.12, FastAPI, Starlette WebSocket, uvicorn |
| DB | PostgreSQL 17 + PostGIS 3 + citext + pg_trgm (image `postgis/postgis:17-3.5`) |
| ORM / migrations | SQLAlchemy 2 async + asyncpg, Alembic (single head, checked in CI) |
| Auth | bcrypt (passlib), JWT access (python-jose) 60 min, rotating refresh token (HttpOnly cookie + family revocation, hash in DB), e-mail codes (6 digits, hashed, 10 min, 5 attempts), Google OAuth (authorization code) |
| Realtime | `pg_notify('delivery_events', …)` after commit → per-process LISTEN → WebSocket hub; per-subscriber read-policy check |
| Jobs | APScheduler in the API process, one leader via `pg_try_advisory_lock` |
| Files | S3 API (MinIO locally, DO Spaces in the cloud): private bucket, presigned PUT/GET; `public/` prefix for shop/hot-deal photos |
| Push | firebase-admin (FCM HTTP v1); provider `log` when no credentials (writes `push_deliveries`, used by tests) |
| E-mail | SMTP (Mailpit locally); `EMAIL_PROVIDER=log` in unit tests |
| WhatsApp / SMS | Meta Cloud API + WinSMS, **off** unless configured (same as today) |
| Maps | Nominatim (geocode), Overpass (OSM refresh), OSRM (ETA), all with timeouts + fallbacks; `places` in PostGIS with trigram search |
| Rate limiting | slowapi per user/IP (generous) |
| Quality | ruff, pytest (+pytest-asyncio, httpx), real Postgres in tests, coverage ≥ 80 % on services |

## 3. Local stack (docker compose, project name `odsdlv`)

| Service | Port (host) | Notes |
|---|---|---|
| `db` postgis | 5451 | db `ods_delivery`, user `ods_delivery`; a second db `ods_delivery_test` for pytest |
| `api` | 8110 | FastAPI, `--reload` in dev; runs `alembic upgrade head` at start |
| `minio` | 9110 (S3), 9111 (console) | buckets `ods-delivery` (private) created by `minio-init` |
| `mailpit` | 8125 (UI), 1125 (SMTP) | every e-mail (OTP, reset) lands here |
| front (ods-delivery, not in compose) | 5190 | `npm run dev:ods` → `VITE_BACKEND=ods`, `VITE_API_URL=http://localhost:8110` |
| `api-prod` (profile `full`) | 8111 | the API image as built: no reload, JSON logs, read-only |
| `front` (profile `full`) | 5191 | `Dockerfile.ods` of the front worktree (nginx), talks to 8111 |

Ports avoid the ODS main stack (8000, 5441, 1337, 3000/3001) and 3033/3034.
`make up`, `make down`, `make migrate`, `make seed`, `make test`, `make lint`;
`make up-full`, backups and reset: `docs/RUNBOOK_LOCAL.md`.

## 4. Repository layout

```
app/
  main.py            app factory, lifespan (db, realtime listener, scheduler, firebase)
  config.py          pydantic-settings (every env var documented in .env.example)
  db.py              engine, session dependency, pool budget, `transaction()` helper
  models/            SQLAlchemy models (one module per area) — mirrors migrations
  migrations/        Alembic
  security/          passwords, jwt, refresh tokens, email codes, google oauth, deps (current_user, require_admin)
  api/               routers: auth, me, compat_entities, compat_functions, files, ws, admin, webhooks, health, dev (local only)
  compat/            legacy document shapes: per-entity serializer, filter translator, read policy, write policy
  services/          domain logic (pure-ish, take a session): orders, offers, dispatch, cancellation, transitions,
                     no_response, hot_deals, messages, notifications, push, whatsapp, sms, places, shops, geocode,
                     eta, referral, reliability, couriers, customers, account_deletion, settings, commission, expiry
  realtime/          events (publish in-transaction), listener, hub, protocol
  jobs/              scheduler + jobs (sweep every 5 min, hot deals hourly, OSM daily, statements weekly)
  storage/           S3 presign, key conventions, public/private
  integrations/      http clients: nominatim, overpass, osrm, fcm, meta whatsapp, winsms, smtp
tests/               pytest: unit (services), api (httpx against the app + real db), realtime, jobs
scripts/             seed_local.py (QA accounts), check_single_head.py
docs/                this file, FIELD_MAPPING.md, COMPAT_GUIDE.md, RUNBOOK_LOCAL.md
docker-compose.yml, Dockerfile, Makefile, .env.example, pyproject.toml, .github/workflows/ci.yml
```

## 5. Data model

Audit §6.2 is the reference DDL. Rules:
- uuid PKs (bigint identity for append-only logs), every reference a real FK
  with an explicit `ON DELETE`.
- `users` = legacy `User` + `UserProfile` merged; `couriers` 1:1 optional.
  E-mail is `citext UNIQUE`, never a FK.
- Money `numeric(10,3)`; timestamps `timestamptz`; geo `geography(Point,4326)`.
- `orders` + `order_stops` + `order_status_events` + `order_offers` +
  `order_tracking` + `order_issues` + `order_ratings` + `messages` +
  `notifications` + `device_tokens` + `no_response_cases` + `hot_deals` +
  `outbound_messages` + `courier_ledger_entries` + `courier_statements` +
  `places` + `shops` + `shop_menu_items` + `shop_reviews` + `app_settings` +
  `audit_log` + auth tables (`refresh_tokens`, `email_codes`) + `files`
  (uploaded objects: key, owner, visibility, content type, size) +
  `push_deliveries` (log provider / delivery audit).
- **Every legacy field the front or a function reads must have a home.** The
  mapping legacy field → column/expression lives in `app/compat/<entity>.py`
  and is documented in `docs/FIELD_MAPPING.md`; a dropped field is listed there
  with its reason (dead, derived, duplicated).
- Counters are derived (`courier_stats`, `customer_stats` views); the compat
  layer exposes them under the legacy names (`total_deliveries`,
  `total_earnings`, `average_rating`, `total_orders`, `no_response_incidents`…).
- `old_import_id` on every imported table (unique, nullable).

## 6. API

### 6.1 Compat surface (what the front uses)

All under `/api`. JSON. `Authorization: Bearer <access>` (the client keeps the
access token in `localStorage`; the refresh
token is an HttpOnly cookie `odsd_refresh`, path `/api/auth`).

- `GET  /api/entities/{Entity}?q=<json>&sort=<-field>&limit=&skip=` — filter
  (q may be absent = list). Supported operators: equality, `$in`, `$nin`,
  `$ne`, `$gt/$gte/$lt/$lte`, `$exists`; fields are **legacy names**, the
  translator maps them to SQL. Unknown field → 400 (never silently ignored).
  Read policy applied in SQL (never filter after LIMIT).
- `GET  /api/entities/{Entity}/{id}`
- `POST /api/entities/{Entity}`, `PATCH /api/entities/{Entity}/{id}`,
  `DELETE /api/entities/{Entity}/{id}` — only where a write policy exists;
  otherwise 403 ("Permission denied…").
- `POST /api/functions/{name}` — body = the payload the front sends today;
  response = the JSON the front expects (fixed keys, status
  codes: 400/401/403/404/409/410/429). One module per function name in
  `app/api/compat_functions/`, thin: validate → call services.
- `POST /api/files/upload` (multipart, public or private) and
  `POST /api/files/signed-url` — back `integrations.Core.UploadFile`,
  `UploadPrivateFile`, `CreateFileSignedUrl` (same return keys:
  `file_url`, `file_uri`, `signed_url`).
- `GET  /api/ws?token=` WebSocket, see §7.
- `/api/auth/*`: `login`, `register`, `verify-otp`, `resend-otp`,
  `reset-password-request`, `reset-password`, `change-password`, `refresh`,
  `logout`, `me` (GET/PATCH), `google/start`, `google/callback`,
  `account-setup` (first login of an account without a password, §9).

Record shape returned for entities: `{ id, created_date, updated_date,
created_by, ...legacy fields }` — dates as ISO-8601 UTC with an explicit
`Z` (`2026-09-28T08:31:58.996000Z`); naive dates sent by the front are read as UTC.

### 6.2 Clean API

The domain services are also reachable through clean routes (`/api/v1/orders`
…) only where the compat surface is not enough (admin statements, webhooks,
health). The front migrates to them later, screen by screen; no duplication of
logic — both call the same services.

### 6.3 Errors

`{ "error": "<code>", "message": "<human text>" }` with the status code. The
client raises an error object shaped like an axios error:
`err.status`, `err.response.status`, `err.response.data`, `err.message`, so the
front's existing handling keeps working.

## 7. Realtime

- A service that changes a row calls `events.emit(session, entity, type, id)`;
  the events are sent with `pg_notify` **after commit** (SQLAlchemy
  `after_commit` hook on the session), payload `{entity, type, id}`.
- Each API process LISTENs (dedicated asyncpg connection, auto-reconnect) and
  hands events to the hub. For each socket subscribed to the entity it loads
  the row once (compat serializer), checks the entity **read policy** for that
  user, and sends `{entity, type, id, data}` (`data` absent for delete).
- Protocol: client → `{op:"subscribe", entity}` / `{op:"unsubscribe", entity}` /
  `{op:"ping"}`; server → events, `{op:"pong"}`, `{op:"error"}`. Auth by token
  query param at connect; closed with 4401 when it expires (client reconnects
  after refresh). Client reconnects with backoff and emits a synthetic
  `{type:"resync"}` so hooks refetch (the existing slow safety polls stay).
- Live courier position: `order_tracking` updates emit `Order` update events
  (the front reads `courier_live_*` from the order today).

## 8. Jobs (leader only)

| Every | Job | Function |
|---|---|---|
| 5 min | no-response sweep, WhatsApp/SMS pending checks, stale order expiry (24 h open / 48 h running), courier presence expiry (online without heartbeat 15 min → offline) | sweepNoResponse, triggerEmergencyContact sweep, sendWhatsAppMessage check_pending, expireStaleOrders |
| (orders) | jobs `expire_stale_orders` (5 min), `courier_presence_expiry` (5 min), `expire_orphan_offers` (1 h) in `app/jobs/orders.py` | expireStaleOrders (+ its hourly `offers` pass) |
| 1 h | expired hot deals, expired offers of closed orders, test data purge | sweepExpiredTestData |
| (incidents) | jobs `sweep_5min` (5 min, no-response sweep), `no_response_fast` (15 s, reminders every 45 s + on-time deadline for waiting cases), `hourly_cleanup` (1 h, hot-deal expiry) and the `hot_deals` purge step in `app/jobs/incidents.py`; full list in COMPAT_GUIDE §4 | triggerEmergencyContact sweep, sweepExpiredTestData |
| 1 day 03:00 | OSM refresh per category (Overpass), off by default locally | refreshOsmIndex |
| 1 week Mon 04:00 (Africa/Tunis) | courier commission statements: due ledger entries of finished weeks → `courier_statements` (job `courier_statements`) | new |

Jobs are idempotent; each also has an admin endpoint to run it by hand.

## 9. Accounts without a password

`users.password_hash` may be NULL (imported accounts, Google-only accounts);
login then answers `409 {error:"account_setup_required"}` and the Welcome
screen switches to "we sent you a 6-digit code" → new password
(`/api/auth/account-setup`). Google users sign in with Google (linked by
verified e-mail).

## 10. Security rules

- Read policies per entity (customer/courier/admin, field-level hides such as
  `customer_phone` on open orders, `id_photo_uri` never exposed,
  `courier_live_*` only to the order's parties).
- Every function keeps its own checks (party of the order, verified courier,
  status guards, limits such as 5 issues per order, 2 active hot deals).
- Admin = `users.role = 'admin'`.
- No internal key: internal calls are Python calls. The cron header
  (`CRON_SECRET`) protects the manual job endpoints.
- Secrets only in `.env` (git-ignored); `.env.example` has placeholders.

## 11. Definition of done (local)

1. `make up && make migrate && make seed` on a clean machine → stack healthy.
2. `make test`: all pytest green, coverage ≥ 80 % on `app/services`.
3. Front `dev:ods` on 5190: customer and courier journeys by hand (order →
   offer → accept → shop → purchase → deliver, chat, notifications, map,
   hot deal, no-response, cancellation, referral, admin verification).
4. The Playwright suites run against the local stack (`BASE_URL=http://127.0.0.1:5190`,
   `ODS_BACKEND=local`) and pass (see `docs/TEST_MATRIX.md`).
