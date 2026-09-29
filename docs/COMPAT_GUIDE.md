# Compat layer guide: adding an entity, a function, an event, a job

The front keeps calling a Base44-shaped client (`ods-delivery/src/api/odsClient.js`,
contract in `ods-delivery-own-backend/docs/OWN_BACKEND_CLIENT.md` §5-7). This backend
answers it from normalized tables. Worked examples to copy:
`app/compat/entities/user_profile.py`, `app/compat/entities/app_settings.py`,
`app/api/functions/getSupportContacts.py`, and their tests (`tests/test_compat.py`,
`tests/test_functions.py`).

## 1. Entities (`/api/entities/{Entity}`)

### What the router does for you
`app/api/compat_entities.py`:
- `GET /api/entities/E?q=<json>&sort=<-field>&limit=&skip=&fields=a,b` → list; `GET /E/{id}`.
  `q` operators: equality (incl. `null`), `$in`, `$nin`, `$ne`, `$gt/$gte/$lt/$lte`,
  `$exists`. Unknown field / operator / wrong value type → `400 invalid_query`. No
  `limit` → 1000 (the cap). Default sort `-created_date`, ties broken by id.
- `POST /E`, `PATCH|PUT /E/{id}`, `DELETE /E/{id}` → your hook; no hook →
  `403 {error:"permission_denied", message:"Permission denied for <op> operation on E"}`.
- After a successful hook it emits the realtime event (`create`/`update`/`delete`) and
  commits, then answers the document as the caller may read it.
- Every route needs a signed-in user (401 otherwise, like Base44's `auth_required`).

The read policy and the field guards are **SQL**: they are applied before
`LIMIT`, in filters and sorts too, so a hidden value can't be probed.

### Declaring an entity
One module per entity in `app/compat/entities/`, imported by
`app/compat/entities/__init__.py` (import = registration).

```python
from app.compat.registry import EntityDef, LegacyField, register

orders = Order.__table__
customer = User.__table__.alias("order_customer")          # alias every joined table

def _parties(user):                                         # a reusable guard
    return or_(orders.c.customer_id == user.id, orders.c.courier_id.in_(my_courier_ids(user)))

ENTITY = register(
    EntityDef(
        name="Order",                                       # the Base44 entity name
        source=orders.join(customer, customer.c.id == orders.c.customer_id)
                     .outerjoin(...),                       # FROM clause (joins for joined fields)
        id_expr=orders.c.id, id_type="uuid",                # "text" when the id is a key (AppSettings)
        created_expr=orders.c.created_at,
        updated_expr=orders.c.updated_at,
        created_by_expr=customer.c.email,                   # optional
        fields={
            "customer_id": LegacyField(customer.c.email, "string"),
            "customer_phone": LegacyField(orders.c.contact_phone_e164, "string",
                                          read_guard=lambda u: or_(_parties(u), admin_clause(u))),
            "total_amount": LegacyField(orders.c.purchase_amount + orders.c.delivery_fee, "number"),
            ...
        },
        read_policy=lambda u: true() if u.is_admin else or_(_parties(u), orders.c.status.in_(OPEN)),
        base_where=true(),                                  # rows that exist as this entity at all
        create=None, update=update_hook, delete=None,       # None = 403 permission_denied
    )
)
```

`LegacyField(expr, type, read_guard=None)`:
- `expr`: any SQL expression over `source` (column, join column, `case`, `func.*`,
  correlated scalar subquery, `jsonb_build_object(..., type_=JSONB)` for objects,
  `func.coalesce(...)`, `func.ST_Y(cast(col, Geometry))` for lat...).
- `type` drives filter coercion and JSON output: `string`, `number` (Decimal → float),
  `integer`, `boolean`, `datetime` (→ naive ISO UTC with microseconds), `id` (uuid; a
  non-uuid filter value simply matches nothing), `object`, `array` (only `$exists` /
  `null` in filters).
- `read_guard(user) -> SQL bool`: the value is `NULL` for users where it is false
  (Base44 field-level read rules: `customer_phone`, `delivery_details`,
  `courier_live_*`, `reported_issues`, `has_issues`, `proposed_by`, ResaleOrder private
  fields...).
- A field that must never leave the server (e.g. `id_photo_uri`) is simply **not
  declared**: it can't be read, filtered or sorted (unknown field → 400).

`read_policy(user) -> SQL bool` is the row-level rule (translate the entity's
`rls.read` from `base44/entities/<E>.jsonc`; `{{user.email}}` comparisons become
uuid comparisons on the FK). Return `false()` for "nobody" (lists come back empty,
`GET /id` answers 404 — the same as Base44 RLS). Admin = `user.is_admin`.

### Write hooks
```python
CreateHook = async (session, user, data: dict) -> str          # returns the new id
UpdateHook = async (session, user, doc_id: str, data: dict) -> None
DeleteHook = async (session, user, doc_id: str) -> Iterable[uuid] | None   # audience of the delete event
```
Rules the two examples follow (keep them):
1. Validate the body with `coerce_payload(ENTITY, data, allowed_fields)`: unknown or
   non-writable keys are **ignored** (the front spreads whole objects into updates),
   writable ones are type-checked (400 `validation_error`). Pick `allowed_fields` per
   caller (owner vs admin).
2. Load the target row `with_for_update=True`; a row the caller may not read → 404,
   readable but not writable → 403 `permission_denied`.
3. Server-owned values are never taken from the body (`updated_by`, owner ids, counters,
   history, fees): compute them.
4. No generic write path: status transitions, amounts, offers, messages go through the
   domain services (`app/services/...`), the same ones the functions call.
5. Flush, don't commit (the router emits and commits). Raise `ApiError` to refuse.

Direct writes the front still does (and which policy each needs) are listed in
`OWN_BACKEND_CLIENT.md` §7; "fallback" rows must answer 403.

### Mapping reference
`docs/FIELD_MAPPING.md` gives the target column / expression of every legacy field.

### Testing an entity
Copy `tests/test_compat.py`: seed rows with `tests/factories.py` (direct DB writes),
call the HTTP routes with `auth(user)`, assert the legacy shape (dates match
`ISO_NAIVE`), the read policy for a stranger, each guard, each hook's refusals.

## 2. Functions (`POST /api/functions/{name}`)

One module per function in `app/api/functions/`, **file name = function name**
(`placeOrder.py`), discovered at import:

```python
AUTH = "user"          # default; "optional" for anonymous callers (getSupportContacts, resolveReferralCode)

async def handle(payload: dict, user: CurrentUser | None, session: AsyncSession, request: Request) -> tuple[int, dict]:
    ...
    return 200, {"success": True, ...}      # same keys and status codes as the Deno function
```
- The body is the payload the front sends (`{}` when empty or not an object).
- Answer with the Deno function's JSON and status (400/401/403/404/409/410/429...).
  Business refusals keep their snake_case codes; never put "rate limit" in a message
  that is not the generic limiter (the front would pause its polls).
- A refusal (status >= 400) is logged at INFO by the router: `function <name> refused <status>
  <error>` (logger `odsd.functions`, no payload values).
- The router commits when the status is < 400 and rolls back otherwise; emit the
  realtime events (below) for every row you change.
- Unknown name → `404 {"error":"function_not_found"}`; the 16 retired names →
  `410 {"error":"gone","function":name}` (`RETIRED` in `app/api/functions/__init__.py`;
  a module with a retired name is refused at start-up).
- Keep the module thin: validation + calls to `app/services/*`.

## 3. Realtime events

```python
from app.realtime.events import emit

emit(session, "Order", "update", order.id)  # inside the transaction that changed it
emit(session, "OrderOffer", "delete", offer.id, audience=[customer_id, courier_user_id])
```
- Sent with `pg_notify('delivery_events', {entity, type, id})` in the same
  transaction: delivered only if it commits, never lost between commit and publish.
- Each API process LISTENs; the hub reads the row **as each subscribed user** through
  the entity's read policy and guards, and sends `{entity, type, id, data, timestamp}`
  only to users who may read it. A delete has no row left to check: pass `audience`
  (user ids; admins always get it), otherwise every subscriber of the entity gets
  `{entity, type:"delete", id}`.
- Live courier position: when `order_tracking` changes, emit `("Order", "update", order_id)`.
- The entity name must be registered in the compat registry to be subscribable.

## 4. Jobs

```python
from apscheduler.triggers.interval import IntervalTrigger
from app.jobs.registry import job
from app.db import transaction


@job("sweep_5min", IntervalTrigger(minutes=5), "no-response sweep, ...")
async def sweep_5min() -> dict:
    async with transaction() as session:
        ...  # idempotent work
    return {"expired_orders": n}  # small JSON summary
```
- Every job of ARCHITECTURE §8 exists (no placeholder left). One module per domain:

| Job | Every | Module | What it does (port of) |
|---|---|---|---|
| `sweep_5min` | 5 min | `app/jobs/incidents.py` | "client ne répond pas" sweep: incident + "last chance" alerts once the deadline is past, auto-close 3 h later; orders in `client_no_response` and orders that left it with a case still open; one transaction per order, SKIP LOCKED (triggerEmergencyContact `sweep`, run by sweepNoResponse) |
| `whatsapp_check_pending` | 5 min | `app/jobs/messaging.py` | SMS for critical WhatsApp not delivered in time, retries (sendWhatsAppMessage `check_pending`) |
| `expire_stale_orders` | 5 min | `app/jobs/orders.py` | open orders idle 24 h → expired, deliveries idle 48 h → abandoned (expireStaleOrders) |
| `courier_presence_expiry` | 5 min | `app/jobs/orders.py` | online couriers without heartbeat for 15 min → offline |
| `expire_orphan_offers` | 1 h | `app/jobs/orders.py` | pending offers of closed orders → expired (expireStaleOrders `offers`) |
| `purge_order_drafts` | 1 h | `app/jobs/orders.py` | order drafts past their 24 h → deleted (new, 2026-09-29; `app/services/order_drafts.py`, functions saveOrderDraft / listOrderDrafts / deleteOrderDraft, placeOrder `draft_id`) |
| `hourly_cleanup` | 1 h | `app/jobs/incidents.py` | listed hot deals past their expiry → expired (announced to the listings) |
| `test_data_purge` | 1 h | `app/jobs/messaging.py` | purge steps; step `hot_deals` (`app/jobs/incidents.py`): expired unsold deals and expired QA deals deleted (sweepExpiredTestData) |
| `osm_refresh` | daily 03:00 | `app/jobs/periodic.py` | OSM places refresh (refreshOsmIndex), off unless OSM_REFRESH_ENABLED |
| `courier_statements` | Mon 04:00 Africa/Tunis | `app/jobs/periodic.py` | weekly commission statements |
- Only the leader process runs them (`pg_try_advisory_lock` on a dedicated
  connection; followers retry every `SCHEDULER_LEADER_RETRY_SECONDS`).
- By hand: `POST /api/admin/jobs/{name}/run` with an admin token or
  `x-cron-token: $CRON_SECRET`; `GET /api/admin/jobs` lists them.

## 5. Services available to ports

- Notifications: `app.services.notifications.notify(session, user_id=, type_=, title_ar=,
  title_fr=, body_ar=, body_fr=, order_id=, metadata=)` → the flushed row (in-app row +
  realtime event + push per preferences + the WhatsApp fallback for web-only opted-in
  customers on `on_the_way` / `new_offer`). `notify_detailed(...)` takes the same
  arguments and also answers `push_skipped` / the push summary / the WhatsApp answer.
  Legacy type synonyms are accepted (`courier_on_way`, `incoming_order`…).
- Push: `app.services.push.send_to_user(session, user_id, PushMessage(...))`
  (checks no preference itself; `notify` decides).
- WhatsApp / SMS: `app.services.whatsapp.send_template(session, template_key=, params=,
  idempotency_key=, to= | user_id=, order_id=, critical=)`, `summary(session, key)`,
  `check_pending(session, order_id)` — `(status, json)` like the Deno actions; OFF (rows
  `disabled`) until WHATSAPP_* / WINSMS_* are set. Templates: `customer_no_response`
  (critical, SMS fallback), `courier_on_the_way`, `new_offer`, `verification_code`.
- Chat: `app.services.messages` (`chat_role`, `courier_user_id`, `load_order`).
- Test data purge: register a step with `@app.jobs.messaging.purge_step("name")`
  (`async def step(session) -> {counter: n}`), run hourly by the `test_data_purge` job.
- E-mail: `app.services.email.send_email(to, RenderedEmail)`.
- Files: `app.storage.s3.presign_get(key, seconds)` (e.g. `getCourierIdPhotos` signs
  `couriers.id_document_key` for admins), `app.storage.keys` conventions.
- Phones: `app.services.phones.to_e164(raw)`, `is_tunisian`, `customer_phone_verified`. Customers:
  a Tunisian number is accepted as is; a foreign one must be confirmed by the WhatsApp code
  (`app/services/phone_verification.py`, functions requestPhoneVerification /
  confirmPhoneVerification) before placeOrder / reserveHotDeal accept it (`phone_unverified`) — only once WhatsApp is
  configured: while it is off the profile's foreign number is accepted as is
  (`phones.verification_enforced()`, hotfix 89577eb) — and
  `whatsapp.send_template` reaches a foreign number only when it is the user's verified phone (no
  SMS fallback abroad). Couriers and shops stay Tunisian-only.
- Counters: views `courier_stats`, `customer_stats` (`app.models.views`); the 180-day
  incident window is `INCIDENT_WINDOW_DAYS`.

## 6. Orders: the transition service (every status change goes through it)

`app/services/order_transitions.py` is the **only** place that writes `orders.status`.

```python
from app.services import order_transitions as ot

order = await ot.lock_order(session, order_id)  # SELECT … FOR UPDATE (None if unknown / not a uuid)
await ot.transition(
    session,
    order,
    "on_the_way",
    actor,
    "triggerEmergencyContact",
    reason=None,
    cancelled_by=None,
    location=(lat, lng),
)
await ot.start(session, new_order, actor, "reserveHotDeal", status="accepted")  # a NEW order
```
- `actor`: a `CurrentUser`, a `User`, a user uuid, or `None` for the system (jobs).
- `source`: the flow's name; it is `status_history[].source` in the legacy shape.
- `reason` / `cancelled_by` ('customer' | 'courier' | 'admin' | 'system'): kept on the event.
  The cancel flows also set `order.cancel_reason` / `order.cancelled_by` themselves.
- Raises `ot.InvalidTransition` when the move is not in `ot.ALLOWED`, or when the target
  needs a courier (`accepted` … `delivered`) and `order.courier_id` is None: set the courier
  **before** moving into a delivery status and clear it **after** moving out (the
  `courier_when_assigned` CHECK is evaluated at each flush).
- It does: the status, `accepted_at` / `delivered_at` / `cancelled_at`, the
  `order_status_events` row, deleting `order_tracking` when the order leaves
  `ot.LIVE_STATUSES` (the legacy CLEAR_LIVE_POSITION), the commission ledger entry at
  `delivered` (`app/services/commission.record_delivery`), the realtime `Order` update.
- It does **not** notify anybody and does not touch offers, stops, hot deals or
  no-response cases: the calling flow does (texts differ per flow; see
  `app/services/order_texts.py`, `order_notices.notify_always_pushed` for notices that must
  be pushed whatever the preferences).
- Hold the lock from the read that decides to the transition (`lock_order` first, then
  your other rows). `expire_stale_orders` locks with SKIP LOCKED and re-checks, so a flow
  holding the lock always wins over the sweep.

Matrix (`ot.ALLOWED`, union of all flows; restrict it for your actor):

| from | to |
|---|---|
| (new) | pending (placeOrder), accepted (hot-deal reservation) |
| pending | offers_received, accepted, cancelled |
| offers_received | pending (last offer gone), accepted, cancelled |
| accepted | at_shop, on_the_way (hot deal: the goods are already bought), pending (courier drops), cancelled |
| at_shop | price_confirmation_needed, purchased, pending, cancelled |
| price_confirmation_needed | at_shop, purchased, pending, cancelled |
| purchased | on_the_way, client_no_response (reported once the goods are bought), pending, cancelled |
| on_the_way | delivered, client_no_response, pending, cancelled |
| client_no_response | on_the_way (courier resumes / customer answered), delivered, pending, cancelled |
| delivered, cancelled | — (final) |

The courier's own writes (`Order.update` from CourierOrderActive) are narrower
(`order_steps.COURIER_STEPS`): accepted → at_shop (→ price_confirmation_needed) → purchased
→ on_the_way → delivered, accepted → on_the_way only for a hot-deal order
(`orders.resale_deal_id` set). **Nothing** leaves `client_no_response` through
`Order.update`: NoResponsePanel's fallback write is refused (403); `courier_resume` must be
done by `triggerEmergencyContact` with `transition(..., "on_the_way", ...)`.

Hooks and helpers for the no-response / hot-deal features:
- `app.services.cancellation.no_response_refresh` is `app.services.no_response.refresh`
  (installed when `no_response` is imported): cancelOrder calls it before judging a
  no-response cancellation (the live code called `triggerEmergencyContact {action:'status'}`),
  then reads the order's latest `no_response_cases` row (`no_response_gate`).
- cancelOrder already handles the linked hot deal (`orders.resale_deal_id`, or
  `hot_deals.original_order_id` / `buyer_order_id`): customer cancel → relisted while not
  expired, courier cancel → expired; it emits `ResaleOrder` updates.
- `app.services.dispatch.dispatch_order(session, order)` (order locked): broadcast again.
- `app.services.offers.demote_if_no_pending_offer(session, order_id, actor, source)`.
- `app.services.couriers.last_activity_expr()`: the "activity" of an order for the 24 h /
  48 h expiry (order writes, status events, no-response case `started_at / resolved_at /
  final_at`; the live position never counts). A no-response step you write keeps the
  order alive.
- Commission: `record_delivery` runs inside the transition to `delivered`; nothing to do
  in the flows (a hot-deal order with a delivery fee is charged like any other).

Notifications of the order steps: the apps keep sending them, as today, through
`sendNotificationIfEnabled` after the call succeeds (`new_offer`, `order_accepted`,
`at_shop`, `purchased`, `on_the_way`, `delivered`; AdminDashboard for
`account_verified` / `account_rejected`). The server sends only what the Deno functions
sent themselves: `new_order` (dispatch), `order_cancelled` (cancelOrder, expiry; always
pushed), `issue_reported` (in-app only).

## 7. Incidents: "client ne répond pas" and hot deals

`app/services/no_response.py` (triggerEmergencyContact) and `app/services/hot_deals.py`
(createHotDeal, reserveHotDeal, listHotDeals, the hot-deal jobs).
- **Lock order**: `ot.lock_order` first, then the case rows (`FOR UPDATE`), then deals. Every
  action holds the order lock from the read that decides to the last write, so a customer
  confirming, a courier reselling / cancelling and the sweep are serialized; the sweep uses
  SKIP LOCKED and looks again at the next run.
- **One open case per order** (`status <> 'resolved'`): guaranteed by that lock (report closes a
  stale open case before opening a new one); the partial unique index `one_open_case_per_order`
  backs it for `waiting`.
- **Incidents** are derived (`customer_stats`, 180 days, `incident_counted`). A case records an
  incident once, at the deadline (`_finalize`), or when the courier resells / cancels after it;
  a late answer, `courier_reached`, `delivered` void it; a `legacy` case (adopted from an order
  parked before the procedure, `messaging_status = 'legacy'`) never counts.
  `orders.mirror_incidents` mirrors `users.is_blacklisted` (≥ 5) and emits `UserProfile`.
- **Realtime**: every case change emits `NoResponseCase` and `Order` (the order's no_response_*
  fields derive from its latest case). A deal leaving the listing (sold, expired, purged) is
  announced as a `ResaleOrder` **delete** (non-admins can no longer read it: HotDealsSection
  reloads), a listed deal as `create` / `update`.
- **Refusals that keep writes**: a flow answering ≥ 400 after writing something that must stay
  (createHotDeal's case refresh, reserveHotDeal marking an expired deal, the resale-race safety
  net) calls `no_response.keep_writes(session)`; the function module then commits before
  answering (the router rolls back every other ≥ 400).
- **Business refusals of triggerEmergencyContact are 409, never 400**: NoResponsePanel falls back
  to a direct `Order.update` on 400 (refused anyway, 403).
