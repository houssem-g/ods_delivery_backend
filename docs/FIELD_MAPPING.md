# Field mapping: Base44 entities → PostgreSQL

Every field of the 15 Base44 entities (`ods-delivery/base44/entities/*.jsonc`), where it
lives now, and whether the front (`src/`) or a live Deno function (`base44/functions`,
the 16 retired ones excluded) uses it. Usage comes from a grep census of both trees on
2026-09-28 (R = read, W = write, W\* = sent to a function that writes it, R\* = read
from a function's answer). A field is **dropped** only when nothing reads it; the
reason is given.

Conventions:
- `t.col` = column; *derived* = computed in SQL by the compat layer; *join* = read
  through a foreign key; the compat entity module (`app/compat/entities/<entity>.py`)
  holds the exact expression.
- Every entity also returns the built-ins: `id` (uuid as text; `AppSettings` uses its
  key), `created_date` / `updated_date` (naive ISO UTC with microseconds, from
  `created_at` / `updated_at`), `created_by` (creator e-mail where it is meaningful,
  else `null`; Base44 no longer returned it and nothing reads it). `created_by_id` is
  read only by `getOrderCourier` (to trust offers made by the server); every offer is
  server-made now, so it is dropped.
- Every user reference (`*_id` holding an e-mail in Base44) becomes a `uuid` FK to
  `users`; the compat layer answers the e-mail again (join `users.email`).
- Every migrated table has `legacy_b44_id` (unique) — `users` has two: the Base44
  `User` id and the `UserProfile` id (`legacy_profile_b44_id`).

Status of the compat entities: `UserProfile`, `AppSettings`, `Message`, `Notification`,
`DeviceToken`, `MessageLog`, `Shop`, `ShopReview`, `PlaceIndex`, `Order`, `OrderOffer`,
`CourierProfile` and the retired `DeliveryTariffs` stub are implemented (`app/compat/entities/`); the others follow this
mapping (docs/COMPAT_GUIDE.md).

## User (built-in) → `users`

| Field | Used | Target | Notes |
|---|---|---|---|
| id | front R | `users.id` | new uuid; Base44 id in `users.legacy_b44_id` |
| email | front/fn R | `users.email` (citext unique) | |
| full_name | front R | `users.full_name` | |
| role | front/fn R | `users.role = 'admin'` → `'admin'`, else `'user'` | `GET /api/auth/me` answers the Base44 shape |
| is_verified | – | `users.email_verified_at IS NOT NULL` | answered by `/api/auth/me` |
| disabled | – | `users.disabled_at IS NOT NULL` | |
| force_password_reset, is_service, collaborator_role, _app_role, app_id | – | dropped | Base44 internals, unused |
| (password) | – | `users.password_hash` | not exportable: NULL after import → account-setup flow |
| (account deletion) | fn W | `users.deleted_at` (+ `disabled_at`) | deleteMyAccount anonymizes instead of deleting: e-mail → `deleted+<id>@deleted.invalid`, name → 'Utilisateur supprimé', phone / password / Google link cleared; orders kept (contact snapshot anonymized), courier row anonymized (`app/services/account_deletion.py`) |

## UserProfile → `users` (+ default `user_addresses` row, `customer_stats` view)

A profile exists when `users.profile_created_at IS NOT NULL` (the front picks the
customer side on its existence). Compat id = `users.id`.

| Field | Used | Target | Notes |
|---|---|---|---|
| user_id | front R/W, fn R | join `users.email` | filter key everywhere; must be the caller at create |
| phone | front R/W, fn R | `users.phone_e164` | normalized to E.164 (TN default) on write |
| role | front R/W | `users.role` (`admin` reads `customer`) | customer/courier; an admin stays admin |
| language | front R/W, fn R | `users.language` | ar / fr |
| default_address | front R/W | `user_addresses.address` (is_default) | |
| country | front R/W | `user_addresses.country` | was silently dropped by Base44 before 2026-09-28 |
| default_lat, default_lng | front R/W, fn R | `user_addresses.location` (ST_Y / ST_X) | |
| governorate, city | front R/W | `user_addresses.governorate / city` | |
| is_active | front R/W (admin) | *derived* `users.disabled_at IS NULL` | admin only; disabling now blocks sign-in and revokes sessions (Base44 enforced nothing) |
| total_orders | – | *derived* `customer_stats.total_orders` | was never maintained (0 everywhere) |
| no_response_incidents | front R, fn W | *derived* `customer_stats.no_response_incidents` | incidents with `incident_counted`, dated `coalesce(final_at, started_at)` within 180 days (getCustomerReliability rule) |
| is_blacklisted | fn W | `users.is_blacklisted` | admin only; never read by the app |
| last_incident_date | fn W | *derived* `customer_stats.last_incident_at` | |
| notification_preferences.order_status_changes | front R/W, fn R | `users.notify_order_status` | the object is rebuilt with `jsonb_build_object` |
| notification_preferences.new_orders | front R/W, fn R | `users.notify_new_orders` | |
| notification_preferences.incoming_orders | fn R | `users.notify_incoming_orders` | no UI toggle; kept because sendNotificationIfEnabled maps a type to it |
| notification_preferences.chat_messages | front R/W, fn R | `users.notify_chat` | |
| notification_preferences.push_notifications_enabled | front R/W, fn R | `users.push_enabled` | |
| referred_by_courier_id | front R/W, fn R | `users.referred_by_courier_id` → `couriers` | set once, never the caller's own courier |
| referred_by_code | front W | `users.referred_by_code` | |
| referred_at | front R/W, fn R | `users.referred_at` | |
| whatsapp_opt_in | front R/W, fn R | *derived* `users.whatsapp_opt_in_at IS NOT NULL` | withdrawing clears the date |
| whatsapp_opt_in_at | front W | `users.whatsapp_opt_in_at` | |

## CourierProfile → `couriers` (+ `courier_stats` view)

| Field | Used | Target | Notes |
|---|---|---|---|
| user_id | front R, fn R/W | join `users.email` via `couriers.user_id` (unique) | |
| full_name | front R/W\*, fn R/W | `couriers.display_name` | |
| phone | front R/W\*, fn R/W | `couriers.phone_e164` | E.164; NULL only for a deleted (anonymized) account |
| cin_passport | front R/W\*, fn W | `couriers.id_document_number` | owner + admin only; '' after account deletion |
| photo_url | front R (CourierCard) | dropped | legacy public ID photo URL; the migration moves the file to the private bucket (`id_document_key`) and the field reads `null` |
| id_photo_uri | front W\*, fn R/W | `couriers.id_document_key` | **never serialized**; admins get signed URLs from `getCourierIdPhotos` only |
| vehicle_type | front R/W\*, fn R/W | `couriers.vehicle` (enum) | |
| max_package_size | fn W (whitelist) | `couriers.max_package` | never sent by the front nor read; kept (audit DDL, default `petit`) |
| price_per_km, min_fee | front R/W\* | `couriers.price_per_km / min_fee` numeric(10,3) | |
| is_online | front R/W\*, fn R/W | `couriers.is_online` | expires with `last_seen_at` (job) |
| current_lat, current_lng | front R/W\*, fn R/W | `couriers.last_location` (ST_Y / ST_X) | `last_seen_at` set with it |
| notification_radius_km | front R/W\*, fn R | `couriers.notification_radius_km` | |
| verification_status | front R/W (admin) | `couriers.verification` (enum) | + `verified_at`, `verified_by`, `rejection_reason`; the only field an admin writes through the entity (the courier's notice is AdminDashboard's own Notification.create) |
| total_deliveries | front R, fn R/W | *derived* `courier_stats.total_deliveries` | stored value was wrong for 7/9 couriers |
| total_earnings | front R, fn W | *derived* `courier_stats.gross_fees` | sum of delivery fees of delivered orders |
| average_rating | front R, fn R/W | *derived* `coalesce(courier_stats.average_rating, 5)` | 5 without ratings, as the front expects |
| late_cancellations | fn R/W | `couriers.late_cancellations` | counter (the rule can't be recomputed from events); never displayed |
| service_governorate | front R/W\*, fn R | `couriers.service_governorate` | dispatch filter |
| service_country | front R/W\*, fn R | `couriers.service_country` | |
| service_city | front R/W\* | `couriers.service_city` | |
| service_start_time, service_end_time | front R/W\* | `couriers.service_start / service_end` (time) | "HH:mm" in the legacy shape |
| referral_code | front R\*, fn R/W | `couriers.referral_code` (unique) | |

## Order → `orders` + `order_stops` + `order_status_events` + `order_tracking` + `order_ratings` + `order_issues` + `no_response_cases` + `courier_ledger_entries`

| Field | Used | Target | Notes |
|---|---|---|---|
| customer_id | front R, fn R/W | join `users.email` via `orders.customer_id` | filter key |
| customer_name | front R, fn R/W | `orders.contact_name` | snapshot at placement |
| customer_phone | front R/W\*, fn R/W | `orders.contact_phone_e164` | guard: customer, assigned courier, admin |
| courier_id | front R, fn R/W | `orders.courier_id` → `couriers` | filter key; kept when the customer cancels (Base44 kept `courier_user_id` so the courier could still open the order), cleared when the courier drops it or the order is abandoned |
| courier_user_id | front R, fn R/W | join `users.email` via `couriers.user_id` | the 17 orders keeping an e-mail without courier lose it (no FK target) |
| courier_name | front R, fn R/W | join `couriers.display_name` | copy dropped (0 drift measured) |
| courier_phone | front R, fn R/W | join `couriers.phone_e164` | |
| courier_photo | front W(null) | dropped | always null since `migrateCourierPhotoCopies` |
| courier_live_lat / lng / at | front R, fn R/W | `order_tracking.location / recorded_at` | guard: customer, assigned courier, admin |
| items_text, quantity, notes | front R/W\*, fn W | `orders.items_text / quantity / notes` | |
| alternatives | fn W (if sent) | `orders.alternatives` | never sent by the front; kept (audit DDL) |
| estimated_price | front R/W\*, fn W | `orders.estimated_price` | |
| package_size | front W\*, fn W | `orders.package` (enum) | |
| shop_name, shop_address, shop_phone, shop_governorate, shop_city, shop_lat, shop_lng | front R/W, fn R/W | *derived* from `order_stops` seq 0 (`name, address, phone, governorate, city, location`) | the first shop was stored twice (418/418 equal) |
| shops[].name / address / lat / lng / items | front R/W, fn W | `order_stops.name / address / location / items` | one row per stop, `seq` = index; always answered (≥ 1 stop: OrderForm sends the main shop as `shops[0]`); the courier can't rename / move a stop |
| shops[].status | front R/W | `order_stops.status` | pending / en_route / at_shop / purchased (+ skipped) |
| shops[].purchase_amount | front R/W | `order_stops.purchase_amount` | |
| shops[].receipt_photo_url | front W | `order_stops.receipt_key` | key of the courier's own **public** upload (MultiShopStatusButton uses UploadFile); answered as its public URL |
| shops[].completed_at | front R/W | `order_stops.completed_at` | |
| current_shop_index | front R/W | `orders.current_stop_seq` | computed by the server (first stop not bought); the client's value is ignored |
| delivery_address, delivery_governorate, delivery_city | front R/W\*, fn R/W | `orders.delivery_address / delivery_governorate / delivery_city` | |
| delivery_details | front R/W\*, fn W | `orders.delivery_details` | guard: customer, assigned courier, admin |
| delivery_lat, delivery_lng | front R/W, fn R/W | `orders.delivery_location` | |
| preferred_time | front R/W\* | *derived* `CASE WHEN scheduled_for IS NULL THEN 'asap' ELSE 'scheduled' END` | |
| scheduled_time | front R | `orders.scheduled_for` | never set in practice |
| status | front R/W, fn R/W | `orders.status` (enum) | writes only through the transition service |
| purchase_amount | front R/W, fn R/W | `orders.purchase_amount` | total paid at the shops |
| price_confirmed_by_customer | – | *derived* `orders.price_confirmed_at IS NOT NULL` | always false today |
| delivery_fee | front R, fn R/W | `orders.delivery_fee` | |
| total_amount | front R/W, fn W | *derived* `purchase_amount + delivery_fee` | 16 stored totals were inconsistent |
| payment_method | fn W | `orders.payment_method` (`cash` only) | |
| payment_status | fn W | dropped (compat constant `'pending'`) | never `paid`, never read |
| receipt_photo_url | front W | *derived* receipt of the current stop (`order_stops.receipt_key`) | written, never read |
| platform_fee | front W | dropped (compat constant `0`) | always 0 since the launch offer |
| courier_net_earning | front W | *derived* `delivery_fee` (fee − 0) | history only |
| ods_commission | front W | `courier_ledger_entries.amount` (commission kinds) | nominal commission |
| ods_commission_status | front W | *derived* from `courier_ledger_entries.kind` | waived_launch → offered_launch, waived_quota → free_quota, due → due |
| cancelled_by | front R, fn W | `orders.cancelled_by` | + admin / system |
| cancellation_reason | fn W | `orders.cancel_reason` | |
| cancelled_at | fn R/W | `orders.cancelled_at` | |
| resale_order_id | front R, fn R/W | `orders.resale_deal_id` → `hot_deals` | |
| geocode_status | fn W | dropped | written, never read |
| distance_km, eta_minutes | front R, fn R/W | `orders.distance_km / eta_minutes` | |
| customer_rating, rating_comment | front R, fn R/W | `order_ratings.rating / comment` | one row per order |
| no_response_reported | front R, fn R/W | *derived* exists `no_response_cases` for the order | |
| no_response_reported_at, emergency_contact_started_at | front R, fn R/W | *derived* latest case `started_at` | |
| emergency_contact_initiated | fn W | *derived* exists case | |
| customer_responded_to_emergency | front R, fn R/W | *derived* latest case `resolution IN ('customer_confirmed','courier_reached')` | triggerEmergencyContact set it for both answers |
| customer_responded_at | fn R/W | *derived* latest case `resolved_at` for those resolutions | |
| no_response_deadline_at, no_response_final_at, no_response_resolution, no_response_case_id | front R / fn R/W | *derived* latest case `deadline_at / final_at / resolution / id` | were copies of NoResponseCase |
| no_response_channels (in_app, push_devices, whatsapp, sms) | front R, fn R/W | *derived* latest case `channels` (jsonb) | |
| reported_issues[] (type, description, photo_url, reported_at, reported_by, courier_id) | fn R/W | `order_issues` (`issue_type, description, photo_key, created_at, reporter_id`) | aggregated as a JSON array; guard: parties + admin; lost on Base44 before 2026-09-28 |
| has_issues | fn W | *derived* exists `order_issues` | |
| status_history[] (status, timestamp, lat, lng, cancelled_by, reason, source) | front R/W, fn R/W | `order_status_events` (`to_status, created_at, location, cancelled_by, reason, source`) | append-only, aggregated in order; client arrays are ignored (only the courier's lat/lng of its last entry is kept on the event); a courier dropping the order is ONE event `→ pending` carrying `cancelled_by`/`reason` (Base44 wrote a `cancelled` then a `pending` entry) |
| preferred_courier_id | front R, fn R/W | `orders.preferred_courier_id` → `couriers` | |
| courier_stats_recorded_at | fn R/W | *derived* `orders.delivered_at` | counters are views: nothing to record |
| last_dispatched_at | fn R/W | `orders.last_dispatched_at` | |
| (new) accepted_at, delivered_at | – | `orders.accepted_at / delivered_at` | missing timestamps (audit §3.3 #17) |

## OrderOffer → `order_offers`

| Field | Used | Target | Notes |
|---|---|---|---|
| order_id | front R, fn R/W | `order_offers.order_id` | |
| customer_id | fn R/W | join `orders.customer_id → users.email` | 5 inconsistent copies disappear |
| courier_id | front R, fn R/W | `order_offers.courier_id` | |
| courier_user_id | front R, fn R/W | join `couriers.user_id → users.email` | |
| courier_name | front R, fn R/W | join `couriers.display_name` | |
| courier_photo | front/fn W(null) | dropped | always null |
| courier_rating | front R, fn W | `order_offers.courier_rating_snapshot` | snapshot on purpose |
| courier_vehicle | front R, fn W | join `couriers.vehicle` | |
| proposed_fee, eta_minutes, distance_km, message | front R/W\*, fn R/W | same-name columns | |
| status | front R, fn R/W | `order_offers.status` (enum) | a withdrawal (`OrderOffer.delete`) keeps the row as `withdrawn`, which is not an entity row any more (reads / events behave like a delete) |
| created_via | fn R/W | *derived* constant `'createOrderOffer'` | every offer is server-made now |

## Message → `messages`

| Field | Used | Target | Notes |
|---|---|---|---|
| order_id | front R, fn R/W | `messages.order_id` | |
| sender_id | front R, fn R/W | join `users.email` via `messages.sender_id` | the 28 CourierProfile ids are mapped to the courier's user at import |
| recipient_id | front R, fn R/W | join `users.email` via `messages.recipient_id`, `''` when NULL | NULL = a customer's message on an open order (no single recipient), answered `''` like Base44 |
| sender_role | front R, fn R/W | `messages.sender_role` | |
| content | front R/W\*, fn R/W | `messages.body` | |
| is_template | fn W | `messages.is_template` | always false; kept (audit DDL) |
| is_read | front R, fn R/W | *derived* `messages.read_at IS NOT NULL` | |

Policies: read = sender, recipient, the order's customer / assigned courier, admin; no
direct write (create is sendOrderMessage's legacy fallback → 403; the sender's update
right of Base44 let him rewrite `recipient_id`, gone).

## Notification → `notifications`

| Field | Used | Target | Notes |
|---|---|---|---|
| user_id | front R/W, fn W | join `users.email` via `notifications.user_id` | 119 CourierProfile ids fixed at import |
| order_id | front R/W, fn W | `notifications.order_id` (SET NULL) | |
| type | front R/W, fn W | `notifications.type` (CHECK list) | synonyms merged: message→new_message, order_delivered→delivered, courier_on_way→on_the_way, incoming_order→new_order (`NOTIFICATION_TYPE_SYNONYMS`) |
| title_ar, title_fr, body_ar, body_fr | front R/W, fn W | same-name columns | |
| metadata | front R/W, fn W | `notifications.data` (jsonb) | keys read by the front: recipient_role, sender_role, status, notification_type, offer_id, shop_name, shop_address, delivery_address, delivery_governorate, distance_km, quick_actions, main_item, is_emergency, preferred_courier, ctx |
| is_read | front R/W | *derived* `notifications.read_at IS NOT NULL` | written by the recipient only (true sets `read_at`, false clears it) |

Policies: read = recipient, admin; create = admin (anyone; pushed, e.g. AdminDashboard's
`account_verified`) or the recipient himself (orderFlow fallback; not pushed); update =
recipient, `is_read` only; delete = nobody.

## DeviceToken → `device_tokens`

| Field | Used | Target | Notes |
|---|---|---|---|
| user_id | fn R/W | `device_tokens.user_id` | |
| token | fn R/W | `device_tokens.token` (unique) | |
| endpoint_hash | fn R/W | *derived* `substr(encode(sha256(token), 'hex'), 1, 32)` | idempotence now by the unique token; still accepted by unregisterDeviceToken |
| platform | fn W | `device_tokens.platform` | |
| provider | fn W | dropped (compat constant `'fcm'`) | always `fcm` |
| role, user_agent | – | dropped | never written |
| device_model, app_version, locale | fn W / fn R (locale) | same-name columns | |
| last_seen_at, last_error, failure_count, is_active | fn R/W | same-name columns | deactivated after 5 failures or a dead-token answer |
| (new) push log | – | `push_deliveries` | one row per device and send |

Policies: read = owner, admin (Base44: admin); no direct write (registerDeviceToken /
unregisterDeviceToken; a token registered by another account moves to the caller).

## ResaleOrder → `hot_deals`

| Field | Used | Target | Notes |
|---|---|---|---|
| original_order_id | fn R/W | `hot_deals.original_order_id` | |
| courier_id | fn R/W | `hot_deals.courier_id` | |
| courier_name | front R\*, fn R/W | join `couriers.display_name` | |
| courier_phone | front R\* (reserveHotDeal answer) | join `couriers.phone_e164` | admin-only in the entity |
| items_text, purchase_amount, discount_percentage, include_delivery, delivery_fee, shop_name | front R\*, fn R/W | same-name columns | |
| discounted_price | front R\*, fn R/W | `hot_deals.price` | |
| shop_address | fn R | `hot_deals.shop_address` | never written today |
| courier_lat, courier_lng | fn R/W | `hot_deals.pickup_location` | admin-only in the entity |
| photo_url | front R\*/W\* | `hot_deals.photo_key` (public upload) | createHotDeal keeps only the courier's own public upload (Base44 took any https URL); a legacy https URL is answered as is |
| status, expires_at | front R\*, fn R/W | same-name columns | |
| buyer_id | fn R/W | join `users.email` via `hot_deals.buyer_id` | |
| buyer_name, buyer_phone, delivery_address | fn W | *derived* from the buyer's order (`orders.contact_name / contact_phone_e164 / delivery_address` via `buyer_order_id`) | write-only today |
| delivery_lat, delivery_lng | – | dropped | never written (the buyer's order holds the location) |
| (new) reserved_at, buyer_order_id | – | columns | `buyer_order_id` replaces `Order.resale_order_id` in the other direction |

Policies: read = deals still listed (`status = 'available'`) for every signed-in user, every
deal for admins (base44/entities/ResaleOrder.jsonc); `courier_phone`, `courier_lat/lng`,
`buyer_id`, `buyer_name`, `buyer_phone`, `delivery_address` admin only. No direct write (403):
createHotDeal, reserveHotDeal, cancelOrder, the hourly jobs. listHotDeals answers only the
public fields + `distance_km`. A deal leaving the listing is announced as a realtime `delete`.

The buyer's order (reserveHotDeal): `purchase_amount` = the deal's discounted `price` (what
the buyer reimburses; Base44 stored the original amount and a separate `total_amount` =
discounted price + fee, which is what the derived `total_amount` answers now), `delivery_fee`
= the deal's fee, `resale_deal_id` = the deal, one stop (the deal's shop, already
`purchased`) when the deal has a shop name, status history `accepted` / `reserveHotDeal`.

## NoResponseCase → `no_response_cases`

| Field | Used | Target | Notes |
|---|---|---|---|
| order_id | fn R/W | `no_response_cases.order_id` | |
| customer_id | fn R/W | join `orders.customer_id → users.email` | |
| courier_id | fn W | `no_response_cases.courier_id` | |
| courier_user_id | fn W | join `couriers.user_id → users.email` | |
| purchase_amount, started_at, deadline_at, status, final_at, resolution, resolved_at, incident_counted, customer_answered_late | fn R/W | same-name columns | |
| push_devices | front R\*, fn R/W | `no_response_cases.channels->'push_devices'` | |
| whatsapp_log_id | fn R | dropped | never written |
| messaging_status | fn R/W | `no_response_cases.messaging_status` | `whatsapp_<status>[/sms_<status>]`, or `legacy` for a case adopted from an order parked before the procedure (never counts an incident) |
| (whatsapp, sms, in_app of the Order copy) | fn R/W | `no_response_cases.channels` (jsonb) | Order.no_response_channels |

Policies: read = admins and the order's parties (customer, courier; Base44: admin only — the
parties read the Order copies, which are derived from this table here); no direct write (403):
triggerEmergencyContact, createHotDeal, cancelOrder, expire_stale_orders and the sweep own it.
No schema change for the incidents port.

## MessageLog → `outbound_messages`

| Field | Used | Target | Notes |
|---|---|---|---|
| channel, purpose, template_name, lang, params, idempotency_key, critical, status, provider, provider_message_id, attempts, error_code, error_message, sent_at, delivered_at, read_at, failed_at, next_attempt_at, fallback_deadline_at, fallback_status | fn R/W | same-name columns | |
| to | fn R/W | `outbound_messages.to_e164` | |
| user_id | fn R/W | `outbound_messages.user_id` | |
| order_id, notification_id | fn R/W | same-name FK columns | |
| parent_log_id | fn W | `outbound_messages.parent_id` | |
| fallback_log_id | fn R/W | *derived* the child row (`parent_id` = this id) | |

Policies: admin read only. `status` values: queued, retry_pending, sent, delivered, read,
failed, disabled, skipped_opt_out, invalid_number, rate_limited; `fallback_status`: none,
pending, sent, failed, disabled, skipped. The idempotency key is claimed by the unique
column (INSERT … ON CONFLICT DO NOTHING); the Deno `duplicate` rows no longer exist.

## Shop → `shops` (+ `shop_menu_items`)

Read: approved shops for every signed-in user, a proposal (pending / rejected) for its
author only, everything for admins (Base44 let anybody list pending proposals; the map
already hid them). Update / delete: admins (an update of `review_status` sets
`reviewed_by` / `reviewed_at`). Create: proposeShop only (403 on the entity).

| Field | Used | Target | Notes |
|---|---|---|---|
| name, address, phone, opening_hours, description, review_status, proposed_at | front R/W\*, fn R/W | same-name columns | |
| latitude, longitude | front R/W\*, fn R/W | `shops.location` | |
| categories | front R/W\*, fn R/W | `shops.categories` (text[]) | |
| photo_url | – | `shops.photo_key` | never written; kept (audit DDL); answered as the public URL |
| osm_id | fn W | `shops.osm_id` (unique) | OSM id or `custom_…` |
| governorate | – | `shops.governorate` | never written; kept (audit DDL) |
| city | fn R | `shops.city` | |
| menu_items[] (name, price, photo_url, description) | front W\*, fn W | `shop_menu_items` (`name, price, photo_key, description, position`) | aggregated in `position` order; `photo_key` = our public upload key, or a legacy https URL kept as is |
| proposed_by | front R, fn R/W | join `users.email` via `shops.proposed_by` | guard: author + admin |
| (searchByBbox reads shop_type, rating, review_count) | fn R | not in the schema | always undefined on Base44: answered 0 / `categories[0]` |
| (new) reviewed_by, reviewed_at | – | columns | set by the admin review |

## ShopReview → `shop_reviews`

The front keys reviews by strings that are not always a shop or place id
(`ShopDetails.jsx`: `shop:<Shop id>` from the map, `place:<name>@<lat>,<lng>` from the
search lists, an OSM id otherwise). The key is stored as sent (`target_key`) and
resolved when possible (`shop:` → the shop; an OSM / `custom_` id → place or shop;
`place:` → the place with that name within 25 m). `shop_id` / `place_id` are therefore
"at most one" (check `num_nonnulls(shop_id, place_id) <= 1`). One review per user and
key, and per resolved shop / place (409 `already_reviewed`).

| Field | Used | Target | Notes |
|---|---|---|---|
| shop_osm_id | front R/W | `shop_reviews.target_key` | filter key of ShopDetails |
| shop_name | front W | *derived* join `shops.name` / `places.name` | NULL for an unresolved key; the body's value is ignored |
| user_id | front W | `shop_reviews.user_id` (the User **id**, as in Base44) | must be the caller when sent (403), forced otherwise |
| user_name | front R/W | *derived* join `users.full_name` | the body's value is ignored ('Utilisateur supprimé' after account deletion) |
| rating, comment | front R/W | same-name columns | rating 1-5 |
| photo_urls | front R/W | `shop_reviews.photo_keys` (public URLs rebuilt in order) | only our public uploads are accepted |

## PlaceIndex → `places`

Admin-only read entity (`id` = the bigint as text); the app reads places through
searchPlaces / searchByBbox / geocodeAddress, refreshOsmIndex writes them.

| Field | Used | Target | Notes |
|---|---|---|---|
| osm_id, name, address, city, governorate, category, phone, opening_hours, source, source_ts | fn R/W | same-name columns | category vocabulary unified (FR) at import: restaurant, pharmacie, supermarché, boulangerie, banque, carburant, hôpital; '' → NULL for the optional texts |
| name_norm | fn W | `places.name_norm` | `app.services.text_norm.normalize_text(name)`: lower, accents and Arabic harakat/tatweel removed, letters/digits runs joined by one space (Arabic kept; the Deno import emptied it) |
| (new) search_norm | – | `places.search_norm` (trigram index) | `text_norm.search_text(name, address, city)`: haystack of the text scores. **The import must fill it** with that function (or leave `''`: refreshOsmIndex / the osm_refresh job backfill every `''` row with name_norm) |
| lat, lng | fn R/W | `places.location` | |
| quality_score | fn R/W (sort key) | `places.quality_score` smallint, **percent 0-100** | Base44 stored 0.5-1.0 (description said 0-100): import `round(value * 100)`; answered on the 0-1 scale everywhere (entity, searchPlaces, searchByBbox `rating`) |

`geocode_cache` (new, no legacy entity): Nominatim answers per normalized
`address|city|governorate` (hits 30 days, misses 24 h), as its usage policy asks.

## AppSettings → `app_settings`

| Field | Used | Target | Notes |
|---|---|---|---|
| key | front R/W, fn R | `app_settings.key` (primary key, also the compat id) | |
| support_phone, support_whatsapp | front R/W, fn R | `app_settings.value->>'support_phone' / 'support_whatsapp'` | re-validated by getSupportContacts |
| updated_by | front W | `app_settings.updated_by` → `users` (answers the e-mail) | forced to the saving admin |

## DeliveryTariffs → dropped (stub entity)

No function reads it (getActiveTariffs is retired) and no price uses it: the fee is the
courier's offer. Not migrated. The admin tab (`TariffSettings.jsx`) lists / edits it, so
the entity is registered as a stub (`app/compat/entities/delivery_tariffs.py`): reads
answer `[]` (GET by id 404), create / update / delete answer
`410 {error:"retired"}` — the tab shows an empty list and its "save failed" toast.
Fields: name, price_per_km, min_fee, commission_type, commission_percentage,
commission_amount, is_active, description — all dropped.

## Order write paths (policy summary)

| Caller | Path | What is accepted |
|---|---|---|
| customer | `placeOrder` | the whitelisted form (`app/services/orders.build_order`) |
| customer | `Order.update` | `delivery_lat / delivery_lng` only while the delivery point is empty (OrderTracking geocode); `shop_*` ignored; any other Order field → 403 |
| assigned courier | `Order.update` | `status` (next step only), `shops[]` progress, `purchase_amount`, `receipt_photo_url`; computed / ignored: `current_shop_index`, `total_amount`, `status_history`, commission fields; any other Order field → 403 (`app/services/order_steps.py`) |
| anybody else | `Order.create / update / delete` | 403 (the §7 fallbacks of OWN_BACKEND_CLIENT.md) |
| functions | createOrderOffer, acceptOrderOffer, cancelOrder, expire_stale_orders, … | through `app/services/order_transitions.transition` |
