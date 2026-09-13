# Backend Stabilization — Frontend-Impacting Changes

This document lists all API contract changes introduced by the backend
stabilization work.  **The frontend must be updated to match before merging
this branch into production.**

---

## 1. Login endpoint — OTP-required response no longer includes tokens

**Endpoint:** `POST /users/auth/login/`

**Affected users:** Privileged restaurant roles (owners, managers, finance) when
`source != 'diner'`. The Dinify admin/superuser arm of this branch was removed in
Phase 0.5 PR-A — platform staff authenticate on the admin plane, and a customer
account can no longer hold a platform role.

**Before (old response when `require_otp=True`):**
```json
{
  "status": 200,
  "message": "Please enter the OTP",
  "data": {
    "require_otp": true,
    "prompt_password_change": false,
    "token": "<jwt-access-token>",
    "refresh": "<jwt-refresh-token>",
    "user_id": "<uuid>",
    "profile": { ... }
  }
}
```

**After (new response):**
```json
{
  "status": 200,
  "message": "Please enter the OTP",
  "data": {
    "require_otp": true,
    "prompt_password_change": false,
    "user_id": "<uuid>",
    "profile": { ... }
  }
}
```

**What changed:** `token` and `refresh` fields are **removed** from the
OTP-required login response.  Tokens are now only issued after OTP
verification.

**Frontend action required:**
- Do **not** store or use tokens from the login response when
  `require_otp == true`.
- After the user submits the OTP, call `POST /users/auth/verify-otp/` with
  `{ "user": "<user_id>", "otp": "<otp>" }`.
- The `verify-otp` response returns the JWT tokens — store them at that point.
- If `prompt_password_change` is `true`, call `POST /users/auth/change-password/`
  with the token from `verify-otp`.

---

## 2. Password reset is now a two-step OTP flow

**Endpoint:** `POST /users/auth/initiate-reset-password/` (NEW)
and `POST /users/auth/reset-password/` (CHANGED)

**Before (old single-step flow):**
- Client called `reset-password` with `{ "phone_number": "..." }`.
- Backend generated a random password, sent it via SMS, and returned 200.
- User logged in with the generated password.

**After (new two-step flow):**

**Step 1 — Initiate:**
```
POST /users/auth/initiate-reset-password/
Body: { "phone_number": "256..." }
Response: { "status": 200, "message": "An OTP has been sent...", "data": { "user_id": "..." } }
```

**Step 2 — Verify OTP and get token:**
```
POST /users/auth/reset-password/
Body: { "phone_number": "256...", "otp": "1234" }
Response: {
  "status": 200,
  "message": "OTP verified. Please set a new password.",
  "data": {
    "token": "<jwt>",
    "refresh": "<jwt>",
    "temp_password": "<random>",
    "prompt_password_change": true
  }
}
```

**Step 3 — Change password (existing endpoint):**
```
POST /users/auth/change-password/
Headers: Authorization: Bearer <token from step 2>
Body: { "old_password": "<temp_password from step 2>", "new_password": "..." }
```

**Frontend action required:**
- Replace the old single-step password reset screen with a two-step flow:
  1. Enter phone number → call `initiate-reset-password` → show OTP input.
  2. Enter OTP → call `reset-password` → use returned `token` and
     `temp_password` to immediately call `change-password`.
- The temporary password is **never** sent via SMS — it is only in the API
  response body.

---

## 3. Token refresh endpoint added

**Endpoint:** `POST /users/auth/token/refresh/` (NEW)

```
Body: { "refresh": "<refresh-token>" }
Response: { "access": "<new-access-token>" }
```

**Frontend action required:**
- Use this endpoint to refresh expired access tokens instead of forcing
  re-login.

---

## 4. HTTP status codes now reflect actual errors

**Affected endpoints (non-exhaustive):**

| Endpoint pattern | Old status | New status | Condition |
|---|---|---|---|
| `POST /users/auth/login/` | 200 | 401 | Wrong password, unknown user, inactive account |
| `GET /users/lookup/...` | 200 | 404 | User not found |
| Various restaurant endpoints | 200 | 400/403 | Validation errors, permission denied |
| Various order endpoints | 200 | 400/401/404 | Errors returned as `response['status']` |
| Finance/report endpoints | 200 | varies | Now uses `response.get('status', 200)` |

**Frontend action required:**
- Review all API error handling. If the frontend checks `response.data.status`
  (the JSON body field) to detect errors, it will still work — the body
  `status` field is unchanged.
- If the frontend relies on the HTTP status code always being `200` (e.g.,
  checking `response.status === 200` before reading data), it must now handle
  `4xx` codes properly.
- Recommended: check `response.data.status` (body) rather than HTTP status
  for backward compatibility with both old and new backends.

---

## 5. Rate limiting on auth endpoints

**Affected endpoints:**

| Action | Default rate | HTTP 429 when exceeded |
|---|---|---|
| `login` | 10/min per IP | Yes |
| `verify-otp` | 5/min per IP | Yes |
| `resend-otp` | 5/min per IP | Yes |
| `initiate-reset-password` | 5/min per IP | Yes |
| `reset-password` | 5/min per IP | Yes |

**Frontend action required:**
- Handle HTTP `429 Too Many Requests` responses gracefully (show a "please
  wait" message or disable the submit button temporarily).
- The `Retry-After` header will indicate when the client can retry.

---

## 6. Anonymous diner entry is capability-only and header-only; a signing key is now required

**Endpoints:** `GET /api/v1/orders/journey/table-scan/` and every anonymous
diner operation (`order-details`, `payment-details`,
`POST /api/v2/orders/initiate/`, `PUT /api/v1/orders/submit/`,
`POST /api/v1/reviews/submit/`).

**Before:**
- A raw table UUID could mint a diner session —
  `GET /api/v1/orders/journey/table-scan/?table=<table-uuid>` returned a session
  token (the transitional `DINER_ALLOW_LEGACY_TABLE_SCAN` grace, default on).
- QR credentials and diner session tokens were also accepted from the query
  string (`?credential=`, `?session=`) and the request body (`session`), not only
  from their headers.
- The diner-capability signing key silently derived from `SECRET_KEY` when
  `DINER_CAP_KEY` was unset.

**After:**
- The **only** input that mints a session is a valid signed QR credential in the
  `X-Diner-Credential` header. A raw `?table=<uuid>` (or any query/body value) is
  ignored and returns a clean `400`. The legacy flag and raw-table resolver are
  removed — no setting re-enables raw scanning.
- QR credentials are read **only** from `X-Diner-Credential`; diner sessions
  **only** from `X-Diner-Session`. Query-string and request-body token transport
  is removed.
- `DINER_CAP_KEY` must be configured in every deployed (`DEBUG=False`)
  environment; the app fails to start (`ImproperlyConfigured`) without it and
  never derives it from `SECRET_KEY` in production.
- These capability responses now carry `Cache-Control: no-store, private`.

**Frontend action required:**
- Scan via a signed credential in the `X-Diner-Credential` header (already the
  current Angular contract) — do not pass `?table=`.
- Send the diner session in the `X-Diner-Session` header on every anonymous
  operation; stop sending tokens in query strings or request bodies.

**Deployment prerequisite (backend/ops):**
- Set a strong explicit `DINER_CAP_KEY` (≥32 chars, ≠ `SECRET_KEY`;
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`) in the UAT and
  production `.env` **before merging** — the auto-deploy's `migrate` and
  `check --deploy` steps otherwise fail closed at settings import. A new key
  invalidates existing sessions (6h TTL — diners rescan) and any QR credentials
  signed with the old derived key; to preserve existing stickers, set
  `DINER_CAP_KEY` to the current derived value
  `hmac(SECRET_KEY, b'diner-capability').hexdigest()`.
- The key must be in the **project `.env` file** — the deploy pipeline
  standardizes deployed runtime secrets on that file and validates it
  directly (as `www-data`, via `RepositoryEnv('.env')`). The file must be
  **readable by the Apache runtime user** (`www-data`) via group-read, with
  no group-write and no 'other' permission bits — currently
  `ubuntu:www-data`, mode `640`. This is exactly how UAT went down on
  2026-07-17: the key was present in `.env`, but the file was `ubuntu:ubuntu`
  mode `600`, so the CLI `migrate`/`check --deploy` steps (run as `ubuntu`)
  passed while mod_wsgi died at settings import with `PermissionError` —
  every request (CORS preflights included) then returned Apache's bare 500.
  Confirmed by running the decouple key check as `www-data`; fixed with
  `chown ubuntu:www-data .env && chmod 640 .env`. (Shell-profile exports are
  equally CLI-only and never reach mod_wsgi.) The deploy workflow now
  enforces runtime parity before the restart — a `.env`
  ownership/permission guard, a `www-data` read-and-resolve probe, a
  `check --deploy` re-run as `www-data`, `apachectl configtest` — and a
  post-restart probe requiring the expected `405` from the login route.

---

## 7. Admin second factor now requires an explicit `method`

**Endpoints:** `POST /admin/v1/auth/verify/`, `POST /admin/v1/auth/elevate/`

**Affected users:** Platform staff on the admin control plane
(`admin.dinifyapp.com`). No deployed consumer exists yet — there is no
`Dinify-Admin` SPA — so this is a contract change with no migration burden. It is
recorded here because it is the contract the eventual admin frontend must be built
against.

**Before:** a single `code` field, with the server guessing the factor by trying
TOTP first and falling back to a recovery code.
```json
{ "code": "123456" }
```

**After:** the factor type is explicit and **required**.
```json
{ "method": "totp",     "code": "123456" }
{ "method": "recovery", "code": "aBcDeF12-gHiJkL34_mn" }
```

**What changed:** the old ordering decrypted the stored TOTP secret before it could
reject a wrong code. `ADMIN_SECRET_ENCRYPTION_KEY` is fail-closed, so if that key
were ever lost or corrupted the decryption raised and the recovery-code branch was
never reached — the two factors, which exist precisely as independent failure
paths, were chained through one key. With `method: "recovery"` no decryption happens
anywhere on the request, so recovery codes now work with the key missing. This is
the difference between a bad afternoon and a permanently locked-out platform.

- `method` accepts `"totp"` or `"recovery"`, case-insensitive.
- There is **no default**. An absent or unrecognised `method` is refused — a
  default would silently reinstate the ordering this change removes.
- A denial is byte-identical whatever the cause: a wrong code, the wrong method for
  the code, an unrecognised method and an unusable encryption key are
  indistinguishable to the caller. `verify/` denies `401`, `elevate/` denies `403`,
  both with `{"status": <code>, "message": "Invalid or expired verification."}`.
  The specific cause is recorded in the audit log only.
- Everything else is unchanged: lockout accounting, the TOTP replay guard, one-shot
  recovery-code consumption, `used_recovery_code` / `recovery_codes_remaining` in
  the success body, and throttling.

**Frontend action required:**
- Send `method` on every call to `verify/` and `elevate/`. Offer the operator an
  explicit "use a recovery code instead" affordance rather than posting one code
  into a single field and hoping the server sorts it out.

**Operators:** the tested key-loss recovery sequence is in
[`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md) under `reset_platform_admin_totp`.

---

## 8. Admin login answers a locked account differently (break-glass path)

**Endpoints:** `POST /admin/v1/auth/login/`, `POST /admin/v1/auth/verify/`

**Affected users:** Platform staff on the admin control plane. Still no deployed
consumer (no `Dinify-Admin` SPA), so this is the contract the eventual admin frontend
must be built against.

**Why:** with one administrator and a discoverable username, the old policy — 5
combined failures → a flat 15-minute lock, and a locked account refusing even a
correct password — let anyone who learned the username hold the platform shut
indefinitely. Lockout is not removed; it now escalates, and it gains a way out that
requires a secret an attacker does not have.

**Before:** while locked, `login/` returned `401` for every password, correct or not.
There was no way to present a recovery code, so the lock was absolute until it lapsed.

**After:** while locked, `login/` still checks the password.

```json
// wrong password while locked — unchanged
401 {"status": 401, "message": "Invalid credentials."}

// CORRECT password while locked — a recovery-only challenge
200 {"status": 200, "message": "Second factor required.",
     "data": {"second_factor_required": true, "recovery_code_required": true}}
```

`recovery_code_required` is new on **every** login response (`false` in the ordinary
case). When it is `true`, `verify/` accepts **only** `{"method": "recovery"}`:

```json
// TOTP against a recovery-only challenge — refused as an ordinary bad code
401 {"status": 401, "message": "Invalid or expired verification."}

// a valid recovery code — clears the lockout and signs in
200 {"status": 200, "message": "Signed in.",
     "data": {"username": "...", "expires_at": "...", "used_recovery_code": true,
              "lockout_cleared": true, "recovery_codes_remaining": 9}}
```

`lockout_cleared` is also new on every successful `verify/` response (`false`
normally).

**New lockout policy:** threshold **10** cumulative failures, then the window doubles
per further failure — 10th → 1 min, 11th → 2, 12th → 4, 13th → 8, 14th → 16,
15th → 32, 16th and beyond → 60 (cap). The counter is cumulative: an elapsed window
does not forgive it, so the next failure re-locks at the next step up. Only a
successful verification, the break-glass path above, or
`manage.py unlock_platform_admin` clears it.

**Honest cost:** while locked, a correct password is now distinguishable from a wrong
one. That is the same oracle an ordinary unlocked login already presents, not a new
class of leak — and denial bodies remain byte-identical, so lockout still never
reveals whether an account exists.

**Frontend action required:**
- Read `recovery_code_required` from the login response and, when `true`, prompt for a
  recovery code rather than an authenticator code.
- Surface `lockout_cleared` after `verify/` so the operator knows the lock is gone and
  that they have one fewer recovery code.

**Operators:** the nuisance-lockout recovery path and the shell unlock are documented
in [`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md).

---

## 9. Diner ordering is refused before a restaurant goes live

**Endpoint:** `POST /api/v2/orders/initiate/` (the anonymous diner branch)

**Affected users:** Diners scanning a table QR at a restaurant whose lifecycle state
is still `onboarding`. Live restaurants are completely unaffected.

**Why:** `onboarding` and `live` were operationally identical, so a restaurant could
take real orders from real diners before anyone had asserted it was ready — which is
what the go-live transition exists to prevent.

**Before:** an onboarding restaurant accepted diner orders exactly like a live one.

**After:**
```json
400 {"status": 400,
     "message": "This restaurant is not open for orders yet. Please check back soon."}
```

Deliberately distinct from the suspended/offboarded refusal, which keeps its existing
wording (`"Sorry, the restaurant cannot accept orders at this time"`) — *not open yet*
and *no longer open* are different facts and a diner should be told which.

**The diner MENU still renders during onboarding.** Only ordering is refused. The
owner needs to preview the real QR → menu experience before launch, and a rendered
menu carrying an explicit "not open yet" message tells a stray scanner more than a
bare 503 would.

**Unchanged:** staff/owner ordering (`source: "admin"`) still works while onboarding —
that is how the owner places the end-to-end rehearsal order the go-live checklist
requires. Such an order is flagged `is_test` server-side and is excluded from all
revenue, dashboard and diner-analytics figures; it cannot be reviewed.

**Frontend action required:**
- Diner app: surface the message as-is. No new state to handle — it is an ordinary
  400 on the existing initiate call.
- Portal: an owner viewing reports during onboarding will see rehearsal orders absent
  from every figure while they still appear on the kitchen board. That is intended.

---

## 10. `dashboard-v2` requires `bucket`, and no longer returns a previous-period comparison

**Endpoint:** `GET /api/v1/reports/restaurant/dashboard-v2/`

**Affected users:** none in practice — every deployed caller sends `bucket` and none
reads the removed fields. That makes this change *safe*, not *non-breaking*: the wire
contract did narrow, and a caller written against the old one would break.

### `bucket` is now required

**Why:** `bucket` was introduced (DASH-PERIOD-00) alongside a legacy `period`
parameter keyed on the caller's UI selection rather than on a granularity — `period=day`
meant "the user picked Day, so bucket by *hour*". Absent, empty and whitespace-only
`bucket` fell through to `period` and *its* fail-open hourly default. The frontend
stopped sending `period`, so it has been removed; with it goes the only fallback.

There is no defensible default left to invent. This endpoint bounds neither the date
range nor the bucket count, so a guessed granularity returns an enormous payload rather
than an error the caller can see — `bucket=hour` over a 200-day range is ~4,800 buckets.

**Before:** omitting `bucket` returned 200 with hourly buckets.

**After:**
```json
400 {"status": 400,
     "message": "Missing bucket; expected one of hour, day, week, month, year"}
```

Same envelope and same accepted-value tail as an unrecognised value, which reads
`Unsupported bucket 'x'; expected one of …`. The lead clause is the only difference, so
a caller can tell "you sent nothing" from "you sent something wrong". The accepted-value
list is derived from the vocabulary itself, so it cannot go stale.

`period` is no longer read at all. A caller still sending it gets the `Missing bucket`
400, never the granularity `period` used to select.

### `previous_totals` / `previous_total` / `previous_series` are gone

**Why:** the server computed a preceding-equal-length window from the date range alone.
The frontend now issues a second call for the comparison basis the user actually
selected, which the server cannot infer. Each card had been running its **entire**
aggregation twice — once per window — so removing it drops **7 queries per dashboard
load** (`revenue` 10→5, `orders` 8→6).

**Before:** `data.revenue` was `{series, previous_series, totals, previous_totals}` and
`data.orders` was `{series, previous_series, breakdown, total, previous_total}`.

**After:** `data.revenue` is `{series, totals}`; `data.orders` is
`{series, breakdown, total}`.

**Unchanged:** the six top-level `data` keys, the primary window's series and totals,
the `at` key format (full ISO-8601 with the `+03:00` EAT offset), the bucket vocabulary,
and the `{status, message}` error envelope.

**Frontend action required:** none — both changes were made after the frontend had
already migrated off `period` and off every previous-period field.

---

## 11. Restaurant dashboard — `num_sales` means SALES now, and it will drop

**Endpoints:** `GET api/v1/reports/restaurant/dashboard/` (v1) and
`api/v1/reports/restaurant/dashboard-v2/` (the `orders` card).

**Affected users:** every restaurant owner/manager who looks at the dashboard.

**No field is renamed or removed.** Two keys change MEANING, three figures change
VALUE, and two keys are added. A client that ignores the additions keeps working.

### The headline number will fall, possibly sharply

`num_sales` was `orders.count()` over a queryset filtered only by restaurant,
`is_test` and date — so it counted abandoned `initiated` drafts, cancellations and
refunds as sales. It now counts only revenue-bearing orders (`SALE_STATUSES` =
served + paid), the same definition every other report already uses via
`sale_orders()`.

**This is the owner's headline figure and it drops on the day this deploys.** The
smaller number is the true one; the old one was inflated. It deserves a release
note rather than a silent change.

### `sales_amount` goes the other way — from permanently `null` to a real figure

It was `Sum('total_cost')` (pre-discount gross) over `payment_status = 'paid'`. No
order ever reaches that status: creation seeds `'pending'` and nothing in the
codebase writes `'paid'`, the order-payment write path having been deleted with
the custodial teardown pending PSP integration. It is now `Sum('actual_cost')`
over sales — `sale_filters`' canonical revenue basis.

### The percentages now share one denominator

Cancellation, refund and payment rates all divide by **orders placed**
(`order_status != 'initiated'`) instead of by the inflated `num_sales`. That makes
the cancellation and refund rates comparable to each other, and stops either from
exceeding 100%.

### New keys

| Key | Type | Meaning |
|-----|------|---------|
| `orders_placed` | int | The denominator every percentage is taken over. |
| `payment_tracking_enabled` | bool | `false` until PSP integration lands. |

`payment_tracking_enabled` is the honest label on a card that cannot work yet:
because nothing writes `payment_status = 'paid'`, `paid_orders` is **0 / 0.0% for
every restaurant, always**. The card keeps its shape so nothing breaks; the flag
lets the frontend caveat or hide it instead of rendering a zero that reads as a
measurement. It flips to `true` in the same change that lands the PSP write path.

### dashboard-v2

`data.orders.total` and `data.orders.series[].count` excluded nothing and so
counted abandoned drafts too. They now count orders placed, on the same definition
as v1. A side effect worth having: `breakdown` can now sum to `total`, which was
previously impossible — an `initiated` order is excluded from `open` and fails
`paid`, so it was counted in the total while appearing in none of the four rows.
`data.revenue` is unchanged (it was already payment-gated).

**Frontend action required:** none to keep working. Optional: read
`orders_placed` to label what the rates are over, and read
`payment_tracking_enabled` to hide or caveat the payment card until PSP.

**Not changed:** the diner, item and peak-hour figures still count drafts.
Rebasing the diner counts is a separate question — they also collapse every
anonymous QR guest into a single phantom customer — and belongs with that surface.

---

## 12. The admin plane issues its own CSRF cookie

**This breaks no existing consumer, because no client can currently perform any
admin write.** Every session-authenticated unsafe route on the control plane answers
`403 CSRF Failed: CSRF cookie not set.` today — the double-submit check was enforced
but the cookie it compares against was never issued. There is nothing deployed that
could regress; this entry records the contract the `Dinify-Admin` SPA must be built
against.

**Endpoints** (browser paths — Apache mounts the admin WSGI app at `/api` and
strips that prefix, so the `admin/v1/...` routes registered in
`platform_admin_app/urls.py` are reached as `/api/admin/v1/...`): issuance at
`POST /api/admin/v1/auth/verify/` and `GET /api/admin/v1/auth/session/`.
Enforcement (unchanged) on every unsafe admin route:
`POST /api/admin/v1/auth/elevate/`, `POST /api/admin/v1/delegations/`,
`POST /api/admin/v1/delegations/<id>/revoke/`,
`POST /api/admin/v1/restaurants/<id>/transition/`.

**Affected users:** Platform staff on the admin control plane
(`admin.dinifyapp.com`). Still no deployed consumer, per §7 and §8.

**Before:** the server issued no CSRF cookie at all. `AdminSessionAuthentication.
enforce_csrf` ran Django's double-submit check on every unsafe method, and it could
only ever fail — so the plane was, in a real browser, read-only.

**After:** the server issues the cookie, under its own name.

| | |
|---|---|
| Cookie name | `__Host-dinify_admin_csrftoken` |
| Header to echo | `X-CSRFToken` — **unchanged**, and unchanged from Django's default |
| `HttpOnly` | `false` — the SPA is meant to read this one |
| `Secure` / `Path` / `Domain` | `true` / `/` / none — required by the `__Host-` prefix |
| `SameSite` | `Strict` |

The name is deliberately **not** Django's default `csrftoken`: the customer plane
uses that, and the `__Host-` prefix makes the admin cookie host-only, so no sibling
or parent domain can plant one the admin plane would read back as its own.

**Frontend action required:**

1. Read the `__Host-` prefixed cookie by name; do not assume `csrftoken`.
2. Echo it in the `X-CSRFToken` header on every unsafe method (`POST`, `PUT`,
   `PATCH`, `DELETE`). Safe methods need nothing.
3. **The token ROTATES on each successful `verify/`.** It is bound to the
   `AdminSession`, the way `django.contrib.auth.login()` binds it. A tab still
   holding a token from a previous sign-in will get `403 CSRF Failed`; it must
   re-bootstrap with `GET /api/admin/v1/auth/session/` and retry. Treat a CSRF `403`
   as "re-bootstrap and retry once", not as "session expired, sign in again".
4. `GET /api/admin/v1/auth/session/` ensures rather than rotates, so it is safe to
   call from any tab at any time — it will not invalidate the token other tabs hold.

---

## 13. Order acceptance requires the quote the diner reviewed

**This breaks any client that submits an order without echoing the quote.**
`PUT api/v1/orders/submit/` now requires a `quote_ref` beside `order`, and
refuses the submission when it is absent, unrecognised, or names a draft priced
under the superseded rules.

```
PUT api/v1/orders/submit/   {"order": "<uuid>", "quote_ref": "<opaque>"}
```

Five refusals, each `HTTP 400` with a machine-readable `reason` beside the
sentence:

| `reason` | when |
|---|---|
| `quote_ref_required` | no acknowledgement was sent |
| `quote_ref_stale` | the saved quote is not the one named |
| `legacy_pricing_version` | the draft was priced before this change |
| `quote_incomplete` | the saved order cannot be itemised in full |
| `no_deliverable_items` | no line on the order is still deliverable |

(The fourth row is new; the last one is a correction — the table previously
printed the Python constant's NAME, `nothing_to_prepare`, rather than the value
the API has always sent.)

**`quote_incomplete`** is refused when a live row's parent is not itself in the
live population. Its amount is part of the saved payable, so no itemised quote
built from the remaining lines can add up to what the diner would be charged —
and the reference is perfectly valid, because that row IS in the fingerprint. The
response already discloses this (`order_details.quote_complete`, below), but a
disclosure a client may ignore is not an invariant, so the server refuses the
acceptance itself. Nothing is repriced, trimmed or rewritten to make the lines
add up.

**WHY AN ACKNOWLEDGEMENT AT ALL.** Correct calculation is not agreement to an
amount. Before this, a client could price an order in the browser, show the
diner that number, and submit — and the server would accept whatever it had
saved, which might not be the number anybody saw. `quote_ref` is derived from
the persisted lines and totals, so it changes the moment they do: echoing it
back is the client stating *this is the amount I showed and the diner accepted*.
It is an acknowledgement, not a credential — it authorises nothing on its own,
and the diner table session remains the sole authority for whose order this is.

**There is no staff or internal bypass, deliberately.** A path that skipped the
check would be a path on which the diner's agreement was never established, and
it would be the path every future caller reached for.

**`Order.pricing_version`** (migration `orders_app/0037`, additive, `db_default`
and `default` both LEGACY) separates drafts priced under the old rules from
those priced under the corrected ones. A LEGACY draft is never repriced or
deleted — it is refused at acceptance and the client re-prices the unchanged
basket. Under the expand-only rule a rollback lands old code on the new schema,
which reads and writes the column not at all; the `db_default` is what keeps an
INSERT from older code valid.

### Additive response fields (no consumer breaks)

`POST api/v2/orders/initiate/` gains, all additive:

- `data.quote` — **the authoritative priced order**: one entry per PARENT line,
  each carrying its extras nested beneath it. `line_actual_cost` is the parent
  alone; `line_total_with_extras` is the parent plus its extras, i.e. what that
  row contributes to the payable. The two are deliberately distinct — conflating
  them double-counts or drops the extras.
- `data.order_details.quote_ref`, `.pricing_version`, `.reference_total_cost`.
- `data.order_details.quote_complete` — whether `data.quote` represents every
  live row the saved payable includes. `false` means the record cannot supply a
  coherent quote, and the order will be refused at acceptance with
  `quote_incomplete`; the saved amounts are never adjusted to make it `true`.

### The `savings` contract changed

`savings` on an order line is now the difference between the **reference** unit
price (pre-discount, including paid modifiers) and the **effective** one, floored
at zero, extended by quantity. It can no longer be negative. It previously
subtracted a figure that included paid modifier costs from one that did not, so a
line carrying a paid modifier reported a NEGATIVE saving — an item recorded as
having been discounted into a larger number. Reports sum `savings`
(`sale_filters.discount_sum()`), so historical totals that included such lines
were understated; no data is rewritten by this change.

---

## 14. Checkout answers are correlated, and `checkout_protocol` is now 3

**Additive. Nothing is removed, renamed or re-typed.** Every field a level-2
client reads keeps its exact current meaning and value.

### Why

D04/C made the acceptance fact durable (`OrderAcceptance`) and resolvable by
intent key. It left three things a recovering client cannot do:

1. **Tell a draft from a pre-D04 acceptance.** `accepted` is a boolean over
   "is there an evidence row", so a genuine draft and an order accepted before
   that table existed both read `false`. Those are opposite instructions — the
   first may still be accepted, the second is already in the kitchen.
   (Separating them does not turn the second into a verdict; see
   `evidence_unavailable` below.)
2. **Validate that an answer belongs to its command.** Both submit successes
   are `{status, message, idempotent}` and name no order, key, scope or
   reference.
3. **See what it actually accepted.** The original `quote_ref` is stored and
   published nowhere, so a client could only recompute one from the order's
   CURRENT rows — a different question, with a different answer the moment
   anything about the order changes.

### What is added

One shared projection, `orders_app.controllers.services.acceptance_result`,
published identically on both surfaces:

- `PUT api/v1/orders/submit/` success → a new top-level `checkout` object,
  beside the unchanged `status` / `message` / `idempotent`.
- `GET api/v1/orders/journey/order-details/` (both `?order=` and `?intent=`) →
  a new `data.checkout` object, beside the unchanged `accepted` /
  `accepted_at` / `checkout_protocol`.

```jsonc
"checkout": {
  "order_id":   "<uuid>",
  "intent_key": "<client_order_id>" | null,
  "scope":      {"restaurant": "<uuid>", "table": "<uuid>"},
  "acceptance": {
    "state":       "accepted" | "not_accepted" | "evidence_unavailable",
    "outcome":     "newly_accepted" | "already_accepted" | null,
    "quote_ref":   "<the ORIGINAL reference the diner confirmed>" | null,
    "accepted_at": "<ISO-8601>" | null
  },
  "current": {
    "order_status": "...", "fulfilment_status": "...",
    "cancelled_at": "<ISO-8601>" | null, "served_at": "<ISO-8601>" | null
  },
  "checkout_protocol": 3
}
```

- **`acceptance.state` is three-valued, and only TWO of the three are
  verdicts.** `accepted` and `not_accepted` are DEFINITIVE — an evidence row
  exists, or the order is still `initiated`.
  **`evidence_unavailable` is a NON-ANSWER**: the order is not a draft and
  nothing records an acceptance, so the server CANNOT DETERMINE whether the
  submission landed. Read it as ignorance, never as acceptance. TWO producers
  reach it and nothing on the row separates them — an order accepted before
  the evidence table existed, and a DRAFT a kitchen write cancelled or
  advanced (the kitchen routes resolve an order by primary key and do not
  guard on `initiated`). **The client instruction is still the conservative
  one — do not accept such an order again** — precisely because one producer
  really is an order in the kitchen. It is never backfilled into an
  acceptance, and it never carries an invented reference or moment.
- **`acceptance.quote_ref` is the stored original**, read from
  `OrderAcceptance` and never recomputed from current rows.
- **`acceptance.outcome` is `null` on a READ.** A read observes; it is not the
  result of an acceptance attempt. The key is always present so the shape does
  not vary.
- **`current` is labelled apart from `acceptance`.** A cancelled or served
  order that was accepted still reads `accepted`.
- Timestamps are explicit ISO-8601 strings on both surfaces, so the submit
  reply and the read are byte-identical rather than depending on which
  renderer path produced them.
- **`acceptance` and `current` describe ONE SNAPSHOT.** The recovery read
  fetches the order and its evidence in a single statement
  (`select_related('acceptance')`), so a client can rely on the two halves
  being consistent with each other. Two statements would not have been: under
  READ COMMITTED each takes its own snapshot, so a submission committing
  between them produced `acceptance.state == accepted` beside
  `current.order_status == initiated` — a correlated answer describing a
  moment that never existed. (`transaction.atomic()` does not close that;
  READ COMMITTED re-snapshots per statement inside a transaction too.)

### `checkout_protocol`: 2 → 3

`CHECKOUT_PROTOCOL_CORRELATED = 3` is a NEW level, not a new meaning for 2. A
client pinned to 2 keeps exactly the promises 2 made; only a client that
recognises 3 may rely on the correlation fields or the three-state verdict.
Widening 2 in place would be the #661 mistake again, and worse here — a
level-2 client reading the old `accepted` boolean is right to treat it as
two-valued, because for it, it is.

### Frontend action required

- Read `checkout_protocol >= 3` before relying on `checkout`.
- Migrate off `accepted` / `accepted_at` onto `checkout.acceptance`. The old
  keys stay, with their old (conflating) meaning, deliberately — but a client
  that keeps reading them still cannot separate a draft from a legacy
  acceptance.
- Validate `order_id`, `intent_key` and `scope` against the command that was
  issued before acting on the outcome.

---

## 15. Kitchen order commands name an action and a precondition (D05)

**Every kitchen order mutation changed shape.** This is a hard cutover: there is
no legacy body form, no optional precondition and no bypass. An old client's
write is refused with a stable reason; its READS are unaffected.

### The three routes

```
PUT api/v1/kitchen/orders/<pk>/fulfilment-status/
    {"action": "advance"|"serve"|"correct"|"recall", "if_revision": <int>}

PUT api/v1/kitchen/orders/<pk>/priority/
    {"priority": true|false, "if_revision": <int>}

PUT api/v1/kitchen/orders/<pk>/cancel/
    {"cancellation_reason": "<enum>", "if_revision": <int>}
```

**`{"fulfilment_status": "<target>"}` IS GONE**, and that is the point rather
than tidying. A TARGET does not identify a command: `preparing` is reachable
forwards from `new` and backwards from `ready`, so a delayed forward request was
executed as a RECALL and silently undid another device. An ACTION names one edge.

**`if_revision` IS REQUIRED ON EVERY COMMAND.** `Order.fulfilment_revision`
(migration `orders_app/0040`, additive, `default=0` AND `db_default=0`) versions
the whole kitchen-order state — fulfilment, cancellation and priority together.
It is compared against the row read under the lock, and it is what closes the
case an action alone cannot: after a serve → recall → serve cycle the source
state is `served` again, so a delayed recall reopened a LATER completion.

**`priority` must be a JSON boolean, always stated.** The omitted-value TOGGLE is
removed: a retried request undid itself, which is the opposite of what a
retryable flag needs. `bool(raw)` is gone with it — `'no'` and `'false'` both
used to mean True.

### Responses

Success is `200` with `outcome` (`applied` | `unchanged`) and `data` — the
current-state projection. An authorised conflict is `409` with `reason` and the
SAME `data` shape, so a client has one thing to reconcile against:

```json
{"id": "...", "fulfilment_revision": 3, "order_status": "pending",
 "fulfilment_status": "ready", "priority": false, "served_at": null,
 "cancelled_at": null, "cancellation_reason": null}
```

Reasons: `kitchen_action_required`, `kitchen_action_unknown`,
`kitchen_precondition_required`, `kitchen_precondition_invalid` (400);
`kitchen_forbidden`, `kitchen_manage_required` (403);
`kitchen_precondition_stale`, `order_is_draft`, `order_cancelled`,
`order_terminal`, `order_state_incoherent`, `illegal_transition`,
`recall_window_expired`, `table_occupied`, `order_scope_mismatch`,
`revision_limit_reached` (409). A 403 carries **no** `data` — it must not become
an oracle for a tenant the caller has no relationship with.

### New refusals a previously-accepted request may now hit

- **An `initiated` DRAFT is refused by every command.** It was fully writable:
  a draft could be walked to `served`, which set `order_status='served'` and made
  something the diner never placed a SALE — after which they could no longer
  place it.
- **A CANCELLED order is terminal on every axis.** The guard existed only on the
  serve branch, so a cancelled ticket could still be prepared and stamped served.
- **Recall has a server-enforced 10-minute window** from the current completion.
  There was no server age rule at all; the 24h Completed feed is VISIBILITY and
  is unchanged.
- **Recall is refused when the table has since been claimed** (`table_occupied`).
- **Incoherent historical rows are refused for manual review**, untouched
  (`order_state_incoherent`) — e.g. a served order carrying cancellation
  provenance. Coherent orders with no `OrderAcceptance` remain fully operable.

### Feeds

`orders/active/` and `orders/completed/` additively publish `order_status` and
`fulfilment_revision` per ticket, and `kitchen_protocol: 1` on the envelope. A
client that does not see the protocol must go READ-ONLY rather than inventing a
revision.

### Cutover

**Backend first, then frontend, and the window between them is a write outage
for the kitchen board.** There is deliberately no grace period: a one-release
optional precondition would leave delayed cancellation, serve-cycle and priority
commands outside the guarantee, and would let an old writer mutate rows without
advancing the token. The migration is additive with a database default, so an
old writer's INSERT still succeeds — that is schema compatibility, not
behavioural compatibility. Sequence the deploy as a hold/drain/update/verify,
preferably outside service.

## Summary of frontend changes needed before merge

1. **Login flow:** Stop reading `token`/`refresh` when `require_otp == true`.
   Get tokens from `verify-otp` instead.
2. **Password reset:** Implement two-step OTP flow using
   `initiate-reset-password` + `reset-password` + `change-password`.
3. **Error handling:** Handle `4xx` HTTP status codes (especially `401`,
   `403`, `429`) instead of assuming all responses are `200`.
4. **Token refresh:** Optionally use `/users/auth/token/refresh/` for session
   extension.
5. **Diner entry:** Scan with a signed credential in the `X-Diner-Credential`
   header (not `?table=`); send the diner session in the `X-Diner-Session`
   header on every anonymous op (not in the query string or request body).
6. **Admin second factor:** send an explicit `method` (`"totp"` or `"recovery"`)
   alongside `code` on `admin/v1/auth/verify/` and `admin/v1/auth/elevate/`.
   Applies to the admin control plane only, which has no frontend yet.
7. **Admin lockout break-glass:** read `recovery_code_required` from the login
   response and prompt for a recovery code when it is `true`; surface
   `lockout_cleared` from `verify/`. Admin control plane only.
8. **Pre-launch ordering:** handle the new 400 on `api/v2/orders/initiate/` for a
   restaurant that has not gone live — surface the message verbatim.
9. **Dashboard:** expect the "Sales" figure to fall (it now excludes drafts,
   cancellations and refunds) and `sales_amount` to become non-null. Optionally
   read the new `orders_placed` and `payment_tracking_enabled` keys. No field was
   renamed or removed — see §11.

---

## Remaining external dependencies and risks

### CI limitations
- **SQLite-backed CI**: `misc_app` and `restaurants_app` tests use Django
  `JSONField.__contains` lookups which require PostgreSQL. These tests cannot
  run in CI until a PostgreSQL service is added to the GitHub Actions workflow.
- **`finance_app` tests** have a bug (`resend_otp(identification='msisdn')`
  leaves `user=None` then tries `user.phone_number`) and call internal payment
  controllers that trigger live Yo API calls. Not CI-ready without fixing and
  mocking.

### Payment provider behavior (cannot be verified from this repo)
- **Yo Uganda sandbox** URL is hardcoded (`sandbox.yo.co.ug`). Production URL
  must be switched via code change or env var before go-live.
- **Pesapal sandbox** URL is hardcoded (`cybqa.pesapal.com`). Same concern.
- **DPO redirect URL** is `https://dinify-web` — incomplete/placeholder.
- **SMS gateway** credentials are passed in URL query strings
  (`yo_integrations.py:send_sms`, `messenger.py:send_sms`). This is the
  vendor's API design, but credentials may appear in HTTP access logs.

### Money/Decimal handling
- All 19 monetary fields across `orders_app` and `restaurants_app` use
  `FloatField` instead of `DecimalField`. Float arithmetic is used throughout
  order calculations. This risks rounding errors on large orders or sums.
  Fixing requires a database migration and serializer audit — too invasive
  for a stabilization pass.

### Permissions
- `OrderPaymentsEndpoint` (the `AllowAny` `initiate-order-payment/` write path)
  has been **RETIRED** — the endpoint, its route, and the
  `OrderPaymentTransaction` controller were deleted. It created a
  `DinifyTransaction` for any order UUID with no authentication, no ownership
  check, and a client-supplied `split` amount. The order-payment path will be
  rebuilt at PSP integration (authenticated, ownership-gated, server-bounded
  amounts, non-custodial). The record-only `DinifyTransaction` model remains
  (subscriptions + Transactions reports).
- `MsisdnLookupEndpoint` uses `AllowAny` — may allow user enumeration by
  phone number. Needs product decision.
