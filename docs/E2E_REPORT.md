# End-to-end validation of ODS Delivery on its own backend (local)

Date: 2026-09-28. Backend branch `feat/e2e` (rebased on `main` = feat/devops + PyJWT),
front branch `feat/own-backend` (worktree `ods-delivery-own-backend`). Nothing was
pushed. Everything ran **locally**: API `uvicorn app.main:app` on 127.0.0.1:8110,
database `ods_delivery` (Base44 test data imported + QA accounts, `make seed`), MinIO,
Mailpit; front `npm run dev:ods` on 127.0.0.1:5190. The browser could not reach Base44
(`base44.app` / `base44.com` mapped to an unroutable address + aborted routes); the
API's own calls to Nominatim / OSRM were switched off in this worktree's `.env`
(`NOMINATIM_ENABLED=false`, `OSRM_URL=`), push provider `log`, WhatsApp / SMS without
credentials (off), e-mail to Mailpit only.

## 1. What was run

| What | Where | Result |
|---|---|---|
| Backend pytest | `make test`, db `ods_delivery_test_e2e` | **538 passed** (536 on `main` after the rebase), coverage 98 %, ruff clean |
| Playwright, chromium | 37 spec files + setup (`ODS_BACKEND=local ODS_BACKEND_PUBLISHED=1`) | **643 passed, 7 skipped, 0 failed** (34.8 min) |
| Playwright, mobile-chrome (Pixel 7) | same | full run: 638 passed, 2 failed, 5 skipped + 5 not run (serial `e2e-order` stopped by the install prompt). After 616520f: `e2e-order` 4 passed / 2 skipped; the `rate-budget-client` case (40.3 vs 40 ops/min in fake time, one extra poll) passes on rerun (38.3). Net: **643 passed, 7 skipped** |
| `npm run test:ods-client` | own-backend-client + own-backend-welcome (mocked API) | 12 passed |
| Exploratory journeys | `ods-delivery-own-backend/scripts/e2e-journeys/` (J1–J7) | all steps pass after the fixes below |

Spec-by-spec classification and adaptations: `ods-delivery-own-backend/docs/TEST_MATRIX.md`.
`rate-budget.spec.ts` is Base44-only (Base44 quota over its socket.io); the two
own-backend specs run with their own config. The 7 skips (both projects): 5 need
`manageTestData` seeding (retired on Base44 as well; covered by
`tests/test_hot_deals.py`, the hot-deal flows of `order-flow-features` / `e2e-order`
and journey J4), `e2e-order` "Courier can cancel…" (skipped unconditionally in the
spec) and "Hot deal flow: prise depuis hot deal" (looks for deals on the courier
dashboard, an older design; skips on Base44 too), plus `roles-and-accounts`
"service unreachable at start-up" (Base44 public-settings call, none in the ods build).

## 2. Journeys (Playwright scripts, two/three browser contexts)

| # | Journey | Result |
|---|---|---|
| J1 | NEW account `qa.new+<n>@example.test`: Welcome → sign-up → OTP from Mailpit → role selection → customer profile (phone, governorate, city, address, map picker) | pass (desktop + 360 px) |
| J2 | Imported account without password (one migrated test account, `wat…@gmail.com`): login → 409 `account_setup_required` → wrong code refused → code from Mailpit + new password → signed in; password works for a fresh login (AR screen) | pass |
| J3 | Order (orderFlow, same code as the form) → **courier dashboard shows it without reload in ~0.2–0.4 s** (WS `Order create`, toast "Nouvelle commande à proximité") → offer from the card → **customer's OrderOffers shows it live in ~2 s** (WS `OrderOffer`) → accept → courier gets `accepted` by WS → chat courier→customer with **unread badge** on the customer side, answer, badge on the courier side → "Arrivé au magasin" → purchase 12.5 TND **with receipt photo upload** → start → live position: WS `Order` frame with `courier_live_*` ~0.1 s after `trackCourierLocation`, markers on the customer map → delivered → rating 5 → notifications list (new_offer, at_shop, purchased, on_the_way, delivered, new_message) and bottom-nav badge → notification preference saved | 14/14, desktop and Pixel 7 |
| J4 | "Client ne répond pas" (customer's alert opens live) → 180 s server deadline → resale unlocked → hot deal published with a photo → **the NEW account sees it in Hot Deals without reload** and reserves it → courier delivers (hot deal: straight "on the way") → original order cancelled, customer reliability `incidents: 1` | pass |
| J5 | Referral link `/Welcome?ref=<code>` → "Invité par …" banner → sign-up #2 → `referred_by_code` recorded, courier stats `referred_count` +1 → **account deletion** of #2 (Profile → Supprimer le compte; login then 401) · the NEW account becomes courier (onboarding, private ID photo) → admin: verification tab shows the **signed** ID photo (unsigned URL → 403) → verified → `account_verified` notification · admin users tab: deactivate / reactivate · admin support numbers → shown on Help · shop proposal (ShopSearch → add shop, address search) → admin approval | pass |
| J6 | Arabic tour (RTL, no horizontal scroll, no French UI word) of 16 key customer / courier screens | pass (only data values such as shop / courier names typed by French-speaking test users) |
| J7 | API killed and restarted while the courier dashboard is open: the client reconnects, resyncs (order created during the outage listed; next event live in ~0.8 s) · API with `ACCESS_TOKEN_MINUTES=1`: the socket is closed 4401 at expiry, `/api/auth/refresh` 200, events keep flowing, user stays signed in | pass |

Cancellations by each side are exercised by `e2e-order` (customer cancels right after
placing; cleanup through `cancelOrder`), `order-flow-features` ("courier cancellation
sends the customer back to the offers page", "offers: … cancel stays reachable") and
J4 / J7 cleanups; FR/AR + 360 px on every page by `qa-pages` (112 cases).

## 3. Bugs found and fixed

Backend (`feat/e2e`, each with a pytest regression test):

| Commit | Bug | Severity |
|---|---|---|
| 553c2b1 | `Message` rows were readable (REST **and realtime**) by the recipient and both parties of the order; Base44's published rule is sender-or-admin and the app never reads the entity. Restricted to sender / admin. | high (data exposure) |
| ccbb30c | Admins never saw any courier ID photo during verification: the compat layer hides the key and answers `photo_url: null`, and the admin hook only asks `getCourierIdPhotos` for couriers with one of them. New non-secret `has_id_photo` field. | high (verification blind) |
| 592c517 | Google start / callback without Google configured answered a raw 503 JSON page to a browser navigation; now 302 → `<calling app>/Welcome?auth_error=google_disabled`. | medium |
| fba5522 | A client leaving while its request body was read (`ClientDisconnect`) produced a 500 with an ERROR traceback; now 499, nothing logged as an error. | low |
| e769c59 | `make seed` now voids the QA accounts' counted no-response incidents / late cancellations: after five hot-deal test runs the QA customer was suspended (`placeOrder` 403 `customer_suspended`, correct business rule) and the next suite failed. | test infra |
| 45138aa | The pytest suite read the developer's `.env` (`NOMINATIM_ENABLED`): geocode tests failed with the safe local setting. | test infra |

Front (`feat/own-backend`, Base44 build unaffected unless stated):

| Commit | Bug | Severity |
|---|---|---|
| c6d4486 | App on `127.0.0.1:5190` + API on `localhost:8110` = cross-site: the SameSite=Lax refresh cookie was never stored, every session died with its first access token (60 min). The API URL now follows the page's loopback spelling. (Production: same-site origins or `SameSite=None; Secure`, see §5.) | high (local) |
| 44ad14e | Admin: request the ID photos of couriers flagged `has_id_photo`; Users tab had no account column (only the phone, empty for most rows). Both builds. | high / medium |
| a7722a0 | Top places (order map picker) and the address search of the shop proposal sent `searchPlaces` without a point when the position was unknown → 400 "Location required", empty lists (Base44 too). Fallback: the chosen governorate's centre. | medium |
| 032b5dd | The unread-chat bubble covered the "Notifs" tab and the add-shop button sat under the bottom nav and the bubble (fixed `bottom-24` ignoring the system-bar inset): add-shop could not be tapped on Android browsers. Both builds. | medium |
| 525ce86 | Welcome ignored `?auth_error=` after a failed Google sign-in (no feedback); message shown FR/AR. | medium |
| 26a5b14 | Reads broken by a full navigation surfaced as "Network Error" (console errors, error toasts, `onError` / rate gate while the page was leaving); they now never settle after `pagehide`. Unit test added. | low |
| b97d25c | The no-response alert vibrated before any tap: Chrome logged "Blocked call to navigator.vibrate" as an error. Skipped until a user activation (both builds). | low |
| 6fc5e19, 2ccc8cd, 18a45c7, ba421a5, 616520f | Test side: `ODS_BACKEND=local` mode + Base44 guard; helpers/specs adapted (PATCH, `/api/auth/me`, cleanups through `cancelOrder`, courier steps, security probes against the own API, install prompts answered in the live fixtures). | — |
| 1e662fd | The journey scripts themselves. | — |

## 4. Remaining issues (not fixed)

| Severity | Issue | Note |
|---|---|---|
| medium (decision) | Receipt photos are **public** uploads (`UploadFile`, unguessable URL readable without auth: 200), exactly as on Base44; nothing in the app displays them. Making them private = `UploadPrivateFile` in `MultiShopStatusButton` + the backend accepting a private key for `receipt_photo_url` and signing it for the parties/admin. | to decide |
| low | `Missing Description or aria-describedby` (Radix dialogs), "Select is changing from uncontrolled to controlled" (CustomerProfile, CourierOnboarding), React Router v7 future-flag warnings: dev-mode warnings, no functional impact. | a11y polish |
| low | The Android/iOS install prompt (after 3 s on mobile web) covers the bottom of the screen, incl. the courier's main action button, until "Plus tard" is tapped (remembered). | UX |
| low | `register` is followed by a `login` that answers 403 `email_not_verified` (by design: it opens the OTP step); shows up as a console error. Same on Base44. | cosmetic |
| info | Stored Playwright sessions: the refresh cookie rotates; a state file replayed by several contexts after 60 min is refused by reuse detection (correct). Run the `setup` project before a long session. | test infra |
| info | `rate-budget-client` "OrderTracking + chat" sits at 38–40 ops/min against a 40 budget in fake time (Base44 budget): one extra poll makes it fail occasionally. | test infra |
| info | Runs of two Playwright processes on the same `dev:ods` server can trigger Vite full reloads (one flaky `qa-pages` case under that condition, green alone and in the final run). | test infra |

## 5. Not testable locally, and how to test it later

| Feature | Why not here | How / when |
|---|---|---|
| FCM push (real devices) | no Firebase credentials locally; provider `log` wrote `push_deliveries` rows (checked by the pytest suite) | staging: `FIREBASE_CREDENTIALS_PATH` set, Android build pointing at the staging API, Playwright `native-push` + a real phone: new order / offer / chat pushes, tap routing |
| WhatsApp / SMS (no-response alert, "on the way", new offer) | Meta / WinSMS secrets absent → rows `disabled` (by design) | staging with the Meta test number and a WinSMS sender, `MESSAGING_DISABLED=false`, then the J4 journey with a real phone as the customer |
| Google sign-in | `GOOGLE_CLIENT_ID` empty (now redirects to Welcome with the reason) | OAuth client for the staging origin, redirect URI `https://<api>/api/auth/google/callback`; check first sign-in (account linked by verified e-mail) and the `?access_token=` return |
| Cross-site cookies in production | local = same-site after c6d4486 | if app and API are on different sites, `COOKIE_SECURE=true` + `SameSite=None`; test refresh after 60 min in the published web app and in the Android WebView |
| Server-side Nominatim / OSRM | turned off here on purpose (no third-party call from the API) | staging with a contact-bearing `OSM_USER_AGENT`: `geocodeAddress` for an order without coordinates, `getOrderETA` routing |
| Overpass OSM refresh job | off (`OSM_REFRESH_ENABLED=false`) | run `osm_refresh` once by hand on staging (`POST /api/admin/jobs/osm_refresh/run`) |

## 6. What the lead should check by hand

1. The **Message** read-policy change (553c2b1): confirm nobody relies on reading the other
   party's rows through `/api/entities/Message` (the app does not; chat goes through the functions).
2. The **receipt privacy** decision (§4).
3. One real phone run of J3 / J4 on the Android shell once pointed at staging (push, WhatsApp).
4. Admin screen with real data volumes: Users tab now shows the account column (44ad14e).
5. Account used for the imported-account check: `wat…@gmail.com` (test data) now has a local
   password; restore from the snapshot if a pristine import is needed.
