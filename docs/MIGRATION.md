# Migration Base44 → PostgreSQL

How the Base44 export becomes rows of our schema, every rule applied on the way, every
exclusion and why, and the cutover / rollback procedure. The field-by-field mapping is
`docs/FIELD_MAPPING.md`; the design context is `docs/ARCHITECTURE.md` §9 and the audit
(`ods-delivery/docs/DB_AUDIT.md` §1-3, §6.6).

## 1. Pipeline

```
export (JSON per entity, files/)          ~/ODS-backups/migration/<date>/   mode 600, never in git
  └─ migrate/transform.py   in memory: ids, e-mails → users, merges, phones, stops, events…
       └─ migrate/import.py  upsert in FK order, one transaction per table, never deletes
            └─ migrate/verify.py   counts, statuses, money, integrity, samples, counters
                 └─ migrate/report.py   <export dir>/_report.md (mode 600, aggregates only)
```

| Command | What |
|---|---|
| `make import-base44 EXPORT_DIR=… DATABASE_URL=…` | the whole pipeline; exit ≠ 0 when verify is red |
| `… ARGS="--dry-run"` | transform + import in one transaction that is rolled back (constraint check, counts) |
| `… ARGS="--files"` | also uploads `files/` to the bucket (`S3_*` settings, private and public keys) |
| `uv run python -m migrate.transform DIR [--out /outside/repo]` | transform only; `--out` dumps the intermediate as JSON (mode 600, refused inside the repo) |
| `uv run python -m migrate.import DIR --database-url URL [--dry-run] [--files]` | transform + import |
| `uv run python -m migrate.verify DIR --database-url URL [--samples N] [--seed S] [--check-files]` | verify only |

Other options: `--fallback-region CH` (repeatable, see phones), `--qa-constants PATH` (the
Playwright constants file; default `../ods-delivery/tests/helpers/constants.ts`, used only
to count QA rows in the report), `--report PATH` (must be outside the repository).

`DATABASE_URL` must be given on the command line (an inherited environment variable is
refused). The target must already be at `alembic upgrade head`.

Prerequisites: the target database exists with the extensions of `docker/db/init.sql`
(citext, postgis, pg_trgm, pgcrypto), `alembic upgrade head` ran, and for `--files` the
bucket is reachable. Nothing contacts Base44: the export is taken beforehand
(ARCHITECTURE.md §9).

### Idempotency

- Every row gets `uuid5(namespace, "<Entity>:<legacy id>")`: a rerun produces the same ids.
- Upsert per table on `legacy_b44_id` (or the natural key: `places.osm_id`, `files.key`,
  `order_stops (order_id, seq)`, `order_ratings/order_tracking (order_id)`,
  `courier_ledger_entries (order_id, kind)`, `app_settings.key`, uuid5 ids for addresses,
  menu items and issues). `DO UPDATE … WHERE (columns) IS DISTINCT FROM (new values)`: a
  second run on the same export reports **0 inserted / 0 updated**.
- `order_status_events` (append-only, no legacy key): an order's history is inserted once,
  when the order has no event yet.
- `audit_log` rows written by the import (`action = b44_migration.adjust | b44_migration.exclude`)
  are inserted once (same action, entity, id, before, after).
- Circular references are closed by two update passes: `users.referred_by_courier_id`,
  `orders.resale_deal_id`.
- **Adoption**: a user that already exists with the same e-mail (for example the local
  seed's QA accounts) keeps its id; every migrated reference is re-pointed to it and it
  receives the legacy ids. Same for its courier row. The import refuses (no change) when
  that account already carries another Base44 id.
- Never deletes. Rows created by the new app are untouched. **But** a migrated row that the
  new app changed is overwritten by a rerun: after go-live, run only `--dry-run` (it shows
  the drift as "updated").

## 2. Rules (audit §6.6, as implemented)

### Accounts
- `User` → `users` (legacy id in `legacy_b44_id`). E-mails compared case-insensitively; a
  second `User` with the same e-mail is excluded.
- `UserProfile` merged into its user. Several profiles for one e-mail (3 in the export):
  the most recently updated wins, a field it leaves empty is taken from the next most
  recent one; `legacy_profile_b44_id` = the winner, `profile_created_at` = the oldest
  profile's date. The others are counted in the report.
- `role`: `admin` if `User.role = admin`; else the profile's role (customer/courier) — the
  compat `UserProfile.role` reads it back unchanged; else `courier` when a
  `CourierProfile` exists; else `customer`.
- `email_verified_at` = the account's creation date when `is_verified` (Base44 has no
  verification date); `disabled_at` from `User.disabled` or a profile `is_active = false`.
- `password_hash` NULL: first login goes through `/api/auth/account-setup` (e-mail code).
- The default address (address, governorate, city, country, coordinates) becomes one
  `user_addresses` row (`is_default`). A country that is not a 2-letter code → NULL.
- Notification preferences → the five `notify_*` / `push_enabled` columns (absent = true).

### Couriers
- `CourierProfile` → `couriers`, keyed by its user. A profile whose e-mail has no `User`
  (accounts deleted in Base44: demo couriers of January) is excluded — nothing references
  them. A second profile of the same user is excluded and its references re-pointed.
- Bounds of the schema: `price_per_km` [0, 50], `min_fee` [0, 200],
  `notification_radius_km` [0, 100] → clamped; the original value is kept in `audit_log`.
- `is_online = false` for everyone (presence is ephemeral, the app's heartbeat sets it
  again); `last_location` = the stored position, `last_seen_at` = the profile's update date;
  `verified_at` NULL (unknown). Referral codes unique case-insensitively (a duplicate → NULL).
- ID photo: `id_photo_uri` → the exported file → `files` row (private, purpose
  `courier_id`, owner = the courier's user) and `couriers.id_document_key =
  private/courier_id/<user uuid>/<uuid5>.<ext>`. A file missing from the export or whose
  bytes do not match its type → NULL (reported). The legacy public `photo_url` is dropped.

### Phones → E.164
Tunisia first (`app/services/phones.to_e164`: 8 digits → +216, `+216 …`, `00216…`). A number
that is not Tunisian is retried in the fallback regions (`--fallback-region`, default `CH`:
the owner's Swiss mobiles in national format `07x…`). A bare country code (`+216`) is blank.
Anything else is **rejected → NULL** and counted (placeholders such as nine digits
`123…`). `couriers.phone_e164` is nullable (catalog revision `5c1d7e2a9b40`, for deleted
accounts); a migrated courier without a usable phone re-enters it at the next profile save.
The migration adds no Alembic revision of its own.

### Orders
- Kept even when they belong to QA accounts (there is no test flag column; the report
  counts them). Excluded only when the customer is unknown, the status is outside the enum,
  `items_text` is empty, or the status needs a courier and none is known (none in the
  2026-09-28 export).
- `courier_id` from the CourierProfile id. Orders that kept `courier_user_id` without
  `courier_id` (the courier withdrew; 20 in the export, all `cancelled`): allowed by the
  CHECK for `cancelled`, so `courier_id` stays NULL — least lossy: the courier is kept as
  `actor_user_id` of the cancellation event (and on his offer).
- Money: numeric(10,3), rounded to the millime. Out of the schema's bounds
  (`delivery_fee` [0, 200], `purchase_amount` [0, 2000]) → NULL, original in `audit_log`
  (2 aberrant fees: 510 and 1 087.76 TND).
- `contact_name` = `customer_name`, else the user's name; `contact_phone_e164` normalized.
- Empty text fields (`''`) → NULL; empty `delivery_address` → `'-'`.
- `quantity` clamped to 1-100; `current_stop_seq` = `current_shop_index` (NULL → 0).
- Timestamps the schema requires:
  - `delivered_at` = last `delivered` history event, else `courier_stats_recorded_at`, else
    `updated_date` (reported);
  - `cancelled_at` = `cancelled_at`, else the last `cancelled` event, else `updated_date`
    (reported: 161 old cancellations had neither);
  - `accepted_at` = last `accepted` event, else the accepted offer's update date.
- `order_stops`: one row per `shops[]` item (`seq` = index); stop 0 also gets
  `shop_phone / shop_governorate / shop_city`. An order without `shops[]` but with a
  `shop_name` gets stop 0 from the `shop_*` fields (96 orders). Stop status outside the
  enum → `pending`; per-stop amount outside [0, 2000] → NULL.
- `order_status_events`: one per `status_history` item (sorted by time; items with an
  unknown status or no timestamp dropped), `from_status` = previous item,
  `source` = the item's source or `legacy`. No history at all → a `pending` event at
  `created_date` plus the final status (source `migration`). History whose last item ≠
  `status` (93 orders) → a synthetic final event (source `migration`) at the terminal time.
  `actor_user_id` = the customer / courier user when the item says who cancelled.
- `customer_rating` → `order_ratings` (courier = the order's courier, rater = the
  customer, date = `delivered_at`). `courier_live_*` → `order_tracking` when the order has
  a courier. `reported_issues[]` → `order_issues`.
- `resale_order_id` → `orders.resale_deal_id` (second pass); a deal not migrated → NULL.

### Offers, chat, notifications, tokens
- `OrderOffer` on a deleted order → excluded. `proposed_fee` outside ]0, 200] → excluded
  (CHECK; the row is kept in `audit_log`, action `b44_migration.exclude`: 22 offers, test
  values such as 1 087.76). `eta_minutes` outside 0-600 → NULL (audit). A snapshot rating
  outside 0-5 → NULL. `decided_at` = update date for accepted/rejected/expired.
  At most one `pending` offer per (order, courier): older duplicates → `expired`; at most
  one `accepted` per order: the one of the order's courier wins, others → `rejected`.
- `Message` on a deleted order → excluded (35). Sender e-mail → user; a CourierProfile id →
  the courier's user; unknown → NULL. Missing `recipient_id` (old rows) → inferred from
  the order: the customer for a courier message; the courier's user for a customer
  message sent after the order was accepted; else NULL (= "customer message on an open
  order", the messaging rule). `read_at` = update date when `is_read`. Body > 1000 → truncated; empty → excluded.
- `Notification`: `user_id` e-mail → user, CourierProfile id → the courier's user; an
  unknown / deleted recipient → excluded (12). Types merged with
  `NOTIFICATION_TYPE_SYNONYMS`; a type outside the list → excluded. A deleted or test order
  id → `order_id` NULL (208). `metadata` → `data` with the legacy ids of `order_id`,
  `offer_id`, `courier_id`, `case_id`, `resale_order_id`, `message_id` replaced by the new
  uuids when the target was migrated (else left as is; `sender_id` left as is).
- `DeviceToken`: the same token twice → the most recently seen row kept.

### No-response cases, hot deals
- `NoResponseCase` → `no_response_cases`; `channels` = the order's `no_response_channels`
  + the case's `push_devices`. A `waiting` case on an order already delivered/cancelled (3)
  → `resolved` (`order_cancelled` / `delivered`, resolved at the order's terminal time;
  audit), otherwise the new sweep job would act on it. At most one waiting case per order.
- `ResaleOrder` → `hot_deals`: original order deleted or a test id (`PW-…`) → excluded (3).
  `price` = `discounted_price` (missing → purchase amount); `sold` without a known buyer →
  `expired`; `buyer_order_id` = the buyer's order pointing at the deal; `reserved_at` =
  update date of sold deals; `pickup_location` = the courier position.

### Catalog, settings, money
- `PlaceIndex` → `places` (upsert on `osm_id`). `name_norm` recomputed for every row with
  `app.services.text_norm.normalize_text` and `search_norm` filled with
  `text_norm.search_text(name, address, city)` — the same rule as the search, **Arabic
  letters kept** (the Deno rule emptied 4 706 Arabic names). Categories
  unified on the app's seven ids (`pharmacy → pharmacie`, `supermarket/grocery →
  supermarché`, `bank → banque`, `bakery → boulangerie`, `hospital → hôpital`, `fuel →
  carburant`, `cafe → restaurant`). `quality_score` (Base44 0.5-1.0) → percent
  `round(value × 100)`; a value above 1 is read as a percentage already.
- `Shop` → `shops` (`review_status` absent → `approved`: published before proposals
  existed; `place_id` linked by `osm_id` when the place exists). `menu_items[]` →
  `shop_menu_items`; the exported menu photo → public key `public/menu/<yyyy>/<mm>/…`; a
  photo that is not in the export keeps its legacy https URL (`app.services.shops.
  stored_photo`, the catalog rule) — it dies with Base44, re-export before closing it.
- `ShopReview` → `shop_reviews` (empty today): `target_key` = `shop_osm_id` as the front
  sent it, `shop:<Base44 id>` rewritten to `shop:<new id>`; resolved to a shop (`shop:`,
  its `osm_id`) or a place (`osm_id`), else kept unresolved like the app does. One review
  per user and key / shop / place (the most recent wins).
- `AppSettings` → `app_settings` (non built-in fields → `value`). Empty today.
- `courier_ledger_entries`: one `commission_waived_launch` entry of 0.500 TND per
  delivered order with a courier and a fee > 0, dated `delivered_at` (launch rule of
  `src/constants/commission.js`: every delivery before 2027-01-01 Tunis is offered).
  A delivery after the launch end gets no entry (the weekly statements job owns it).
- `audit_log`: every value the import changed or row it dropped for a constraint keeps
  its original (`before`) — admins can review them.

## 3. Exclusions (2026-09-28 export)

| Entity | Rows | Reason |
|---|---|---|
| CourierProfile | 2 | account deleted in Base44 (demo couriers, no `User`, nothing references them) |
| OrderOffer | 7 | order deleted in Base44 (dangling `order_id`) |
| OrderOffer | 22 | `proposed_fee` > 200 (CHECK) — row kept in `audit_log` |
| Message | 35 | order deleted in Base44 |
| Notification | 12 | recipient account deleted |
| DeviceToken | 2 | same token registered twice (latest kept) |
| ResaleOrder | 3 | original order deleted / Playwright test id |
| DeliveryTariffs | 0 | dead table, never migrated (FIELD_MAPPING.md) |
| MessageLog | 0 | WhatsApp/SMS log, never configured; not migrated |

Every other exported row is migrated. The per-run numbers are in the report.

## 4. Verify

`verify.py` fails (exit 1) unless all of these hold:
- each entity: rows kept by the transform are in the database (by legacy id) and
  export rows = kept + excluded; derived tables (stops, events, ratings, tracking,
  issues, ledger, addresses, menu items, files) have the expected counts;
- orders / offers / cases / deals per status, notifications per type (synonyms merged);
- sums re-derived **from the raw export** with the rules above: delivery fees, purchase
  amounts, offer fees, hot-deal prices and amounts, ledger (count, 0.500 each, nothing due);
- integrity: courier present when the status needs one, delivered/cancelled dates, every
  migrated order has events and its last event is its status, event chains, contiguous
  stops, rating courier = order courier, no waiting case on a closed order, no sold deal
  without buyer, every FK validated, one default address per user;
- N random orders (seeded) compared field by field with the export after mapping
  (customer, courier, status, items, quantity, amounts, created date, phone, package,
  stops, stop 0 name, rating, offers);
- `courier_stats` = delivered orders and ratings recomputed from the export;
- `--check-files`: every migrated file key exists in the bucket.

## 5. Report

`<export dir>/_report.md`, mode 600, layout in `docs/MIGRATION_REPORT_TEMPLATE.md`:
aggregates, reasons and masked examples only (`abc…@domain`, `9 digits, …89`), never in
the repository.

## 6. Cutover checklist (audit §6.6, adapted)

Before the day:
1. Rehearse on a fresh export into an empty database (`ods_delivery_rehearsal` locally,
   then staging): `make import-base44 … ARGS="--files"` twice — verify green, second run
   0 changes. Read the report; every new exclusion reason must be understood.
2. Front `dev:ods` smoke tests on the rehearsal database with the QA accounts.
3. Announce the window (in-app + WhatsApp the day before).

On the day (T-0 at ~04:00):
1. Publish the Base44 "maintenance" build → no more writes. Disable the Base44
   automations (`sweepNoResponse`, OSM refresh) at the same moment.
2. Final export (`migrate/export.mjs`, ~3 min) into `~/ODS-backups/migration/<date>/`.
3. `pg_dump` of the target database (empty schema at head) — the rollback point.
4. `make import-base44 EXPORT_DIR=… DATABASE_URL=<prod> ARGS="--dry-run"`: constraints
   pass, counts as expected.
5. `make import-base44 EXPORT_DIR=… DATABASE_URL=<prod> ARGS="--files"`; then once more:
   **0 changes**, verify **GREEN**. Criterion: counts = export minus the listed exclusions.
6. Point the `delivery.…` route to the new front (Traefik), switch the WhatsApp webhook
   and Google OAuth redirect, keep FCM.
7. Smoke tests with the QA accounts: order → offer → accept → deliver, chat, push, map;
   an account-setup login.
8. Open. From now on never run the import against this database again (only `--dry-run`).

## 7. Rollback

- Before opening: restore the `pg_dump` of step 3 (or drop and recreate the database and
  `alembic upgrade head`); the uploaded objects can stay (private keys, unreferenced).
- After opening (decide within 24 h, audit §6.6): Base44 stays intact and read-only for 2
  weeks — put the domain redirect back to Base44 and republish the normal build. Writes
  made on the new database meanwhile must be replayed by hand; `audit_log` and
  `created_at > <cutover>` list them.
- Schema: the migration has no revision of its own. Downgrading the catalog revision
  writes a placeholder into NULL courier phones.
