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
- Placeholders for the four ARCHITECTURE §8 jobs are in `app/jobs/placeholders.py`:
  replace their bodies (keep names and triggers).
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
- Phones: `app.services.phones.to_e164(raw)`.
- Counters: views `courier_stats`, `customer_stats` (`app.models.views`); the 180-day
  incident window is `INCIDENT_WINDOW_DAYS`.
