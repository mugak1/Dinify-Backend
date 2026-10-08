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

*Narrowed since (TEST-RESTAURANT-PARITY-00):* that exclusion now applies only at a
restaurant that is not itself classified test. A test restaurant's orders are all
flagged `is_test` and still count in its own figures and can be reviewed. The rule
is in `orders_app/controllers/test_orders.py`.

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
`order_accepted_while_waiting`, `revision_limit_reached` (409). A 403 carries **no** `data` — it must not become
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
- **A command formed against a DRAFT is refused even if the diner places the
  order while it waits** (`order_accepted_while_waiting`). Submission holds the
  same table lock and does not advance the revision, so without this a cancel
  aimed at a draft applied to the just-placed order.

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

### 15a. Completion (post-#319): one observation route, and priority is scoped

Three follow-ups to §15. Only the second changes an answer a deployed client
could already be getting.

**NEW, ADDITIVE — `GET api/v1/kitchen/orders/<pk>/state/`.** The per-order
OBSERVATION that settles an uncertain command. A command whose reply is lost
leaves a client unable to say whether the server acted, and the commands whose
outcome matters most — a cancellation, and a serve past the 24h Completed
window — are exactly the ones that REMOVE the order from BOTH feeds, so "it is
not on the board" is not an answer. It returns the same
`{status, message, kitchen_protocol, data}` envelope and the SAME projection
every command answers with, for ANY order it can see: cancelled, served,
terminal or draft. Eligibility is a question about what may be COMMANDED and
stays with the command routes. It takes no lock, opens no transaction, writes
nothing and repairs nothing, and it makes no claim that an earlier command
caused what it reports.

**EVERY REFUSAL IS ONE NON-DISCLOSING 404** — an unknown id, a malformed one, a
soft-deleted order, AND an order at a restaurant the caller cannot see, in
status and in body alike. That last case is where this READ parts company with
the three command routes, which answer `403 kitchen_forbidden`: on a read,
403-for-foreign beside 404-for-unknown is an existence oracle over the whole
orders table, free and silent for any authenticated kitchen user, and it would
contradict this repository's rule that a tenant-scoped detail read answers 404
"so existence is not confirmed". **The command routes are unchanged and still
answer 403**; that asymmetry is deliberate here — `kitchen_forbidden` is a
client-visible reason the board renders — but it is the same exposure to a
caller willing to attempt a mutation instead of a read, and narrowing it is its
own contract decision.

It is DELIBERATELY ABSENT from the delegated `ALLOWED_ROUTES` — a delegated
session can issue none of the three commands, so it can never hold an uncertain
one to reconcile (the reasoning is recorded in `delegation_scopes.py`).

**A NARROWING — priority now applies only to a ticket the kitchen is still
working on.** `PUT kitchen/orders/<pk>/priority/` on a ticket whose
`fulfilment_status` is `served` was a 200 and is now
`409 illegal_transition`. The flag is meaningless on a completed ticket, but
that is not why it is refused: applying it BUMPED THE REVISION, and a served
ticket is recall-eligible for ten minutes — so a stray priority tap spent the
precondition an operator was holding and their recall came back stale with the
window running down. No shipped board offers the control there (the Completed
card renders Recall only), so no current client is affected.

**INTERNAL — `KitchenCommand` validates on construction.** The revision's type
and range, the `priority` boolean and the `cancellation_reason` vocabulary were
enforced only by the three `parse_*` functions, so a caller that built a command
another way reached `execute` with none of them applied — and `execute` checked
only the ACTION, despite documenting that it self-guards a direct caller. Those
rules now live in `__post_init__`, raising the same `KitchenRefusal` the parsers
raise, and cross-action fields (an `advance` carrying a `cancellation_reason`)
are unrepresentable rather than ignored. `_assert_revision` also re-asserts the
type at the compare-and-set: `!=` alone reads as exact while `False == 0` and
`1.0 == 1` are both true in Python, so either value satisfied a precondition it
had never been checked against. **No HTTP request shape changes** — every one of
these was already enforced at the parser for a request arriving over the wire.

## 16. A saved quote has a lifetime, and acceptance re-checks the agreed purchase (D06)

D06 defines and enforces the conditions under which a saved draft may become a
newly accepted restaurant order. Before it, three whole classes of condition were
checked once, in the controller preflight, on instances loaded in autocommit
**before any transaction opened** — and never again. Nothing re-read them after
the blocking table-lock acquisition, and nothing consulted them at acceptance at
all. Every item below was reproduced over real HTTP on unmodified `main`.

### What was actually happening

* **A pause did not pause.** An owner setting `accepting_orders = False`
  mid-service stopped NEW drafts, and every draft already initiated still
  reached the kitchen. The settings copy said the switch stopped ordering; it
  stopped roughly half of it.
* **A table taken out of service, disabled or switched to `menu_only` still
  accepted an order** that had been initiated while it was usable — and the
  staff/admin entry points checked no table fact at all, so a staff-origin order
  could be placed onto a table that had been removed.
* **No catalogue fact was re-read at acceptance.** A draft priced against a dish
  that was subsequently sold out, unpublished, re-configured or re-tagged for
  allergens was accepted unchanged, and the kitchen worked from a definition
  nobody had agreed to.
* **A saved quote had no lifetime.** A draft priced last week was acceptable at
  last week's prices, indefinitely.
* **Three writers silently reverted committed policy.** A full-row
  `restaurant.save()` in the menu-approval path wrote back `accepting_orders`
  and `status` from an instance loaded earlier; a full-row `table.save()` in the
  table-status action wrote back `qr_version`, **un-revoking every diner
  credential a QR regeneration had just revoked**; and the table deletion
  blocker was evaluated outside the lock the delete then took.

### The request contract — what changes for a client

**`PUT api/v1/orders/submit/` is unchanged in shape.** It takes the same
`{order, quote_ref}` it has taken since D02, with the same authority. What
changes is the set of answers it can give.

Four new refusals, all **HTTP 400** with the established
`{status, message, reason}` envelope that `orders/submit/` refusals already use:

| `reason` | meaning | client action |
|---|---|---|
| `restaurant_paused` | the restaurant has paused new diner ordering | TRANSIENT — the same attempt may succeed later |
| `table_ordering_unavailable` | this table's QR mode does not permit ordering | TRANSIENT |
| `table_unavailable` | the table is soft-deleted, disabled, inactive or out of service | TRANSIENT |
| `restaurant_unavailable` | the restaurant is soft-deleted | TRANSIENT |
| `quote_expired` | the reviewed amount is older than the server's quote lifetime | **TERMINAL** — re-price; the old quote is now retired |
| `quote_unverifiable` | the server cannot establish how old this quote is | re-price; nothing was retired |
| `purchase_needs_review` | something about the quoted purchase changed | **TERMINAL** — re-price; the old quote is now retired |
| `quote_closed` | this quote was already retired by an earlier attempt | re-price |

The first four carry the **byte-identical messages** the preflight has always
returned for the same conditions; only the machine `reason` is new, and it is
additive. A client that reads `message` and ignores `reason` behaves exactly as
before.

**The three staff-origin behaviour changes.** A staff/admin-origin order is
still exempt from the two ORDERING POLICY gates — `accepting_orders` and
`qr_mode` — because taking an order on a diner's behalf is what that exemption
is for. It is **no longer exempt from table or restaurant LIVENESS**: an order
cannot be placed onto a table that has been removed, whoever is asking. Two
tests that pinned the old behaviour were updated rather than deleted, and the
two that pin the surviving exemption are unchanged.

### New: `PUT api/v1/orders/retire-quote/`

Body `{order, quote_ref}`. Same authority as `submit` — the diner table session
bound to the order's table, or a staff caller with the `tables` module.

It asks the server whether a saved quote can still be honoured and retires it if
it cannot. **It never retires a quote that is still good**: the client supplies
no reason and cannot, and a quote the server finds acceptable comes back
`quote_still_valid` with nothing written. It exists because the two obvious
alternatives are both wrong — minting a replacement quote unilaterally leaves the
old one acceptable (so a queued acceptance can still land, and the diner buys the
meal twice), and attempting an acceptance to read the refusal **succeeds** when
the quote is fine, claiming a table and sending food to a kitchen in order to ask
a question.

```
200 {"status": 200, "outcome": "quote_still_valid", "quote_policy": {...}}
200 {"status": 200, "outcome": "quote_closed", "reason": "quote_expired",
     "quote_policy": {...}, "quote_closure": {...}}
200 {"status": 200, "outcome": "quote_already_closed", "reason": ...,
     "quote_closure": {...}}
409 {"status": 409, "reason": "order_already_accepted", "checkout": {...}}
```

The controller consults no lifecycle state and no operational rule, takes no
admission advisory lock and changes no order status: a restaurant that has
PAUSED is exactly when a client most needs to establish that its held quote is
dead. Same asymmetry the admin plane's owner-invitation cancel already draws.
A suspension, an offboarding and a soft-deleted restaurant reach it too — none
of them touches the table, so the diner's session stays live.

An UNAVAILABLE TABLE is the one case the route cannot answer, and that is the
capability channel's rule rather than this controller's: a soft-deleted,
disabled, deactivated or out-of-service table revokes the diner's table session,
so the endpoint answers the channel's opaque 404 and the controller is never
entered. Deliberate — a revoked session must not drive a durable write, and no
replacement quote can be minted at that table either, so there is no purchase
for a closure to protect. A client should treat the 404 as an unanswered round
trip and retry rather than submitting.

### New response keys (additive, on the existing order read)

```
"order_details": {
   ...
   "quote_protocol": 1,
   "quote_policy": {"version": 1, "status": "live", "expires_at": "…"}
}
```

**`quote_protocol` is a SEPARATE LEVEL from `checkout_protocol`, which stays
3 and is not touched by D06.** They answer different questions — one is "can an
uncertain checkout be retried and recovered", the other is "may this quote still
be accepted" — and a client can want either without the other. Raising
`checkout_protocol` for a change that added nothing to what it promises would be
§14's mistake in a new place, and in the direction that matters most: a client
pinned to level 3 is RIGHT that level 3 said nothing about quote lifetime.

**`quote_policy` is a DEADLINE, not a reservation.** Version 1 is **30 minutes
from the order's `time_created`**, and it promises exactly one thing: the
MONETARY figures the diner reviewed will be honoured within it. It does not
reserve stock, does not promise the restaurant is open or the table usable, and
does not guarantee any particular request will be accepted. `expires_at` is
published so a client can stop waiting before it matters; `status` is the verdict
at the instant the response was built, and the acceptance transaction — which
reads its clock after its locks — is the one that decides.

### Migration

`orders_app/0041_order_quote_closure` — **one new table, nothing else.** No
existing table is touched, no column is added or altered, there is no `RunPython`
and no backfill: a draft that predates it was never closed under this protocol,
so an absent row means "unknown, or not closed" and never "safe to replace".

**The rollback direction needs an operational decision rather than a revert.**
The SCHEMA is safe under the expand-only rule — old code neither reads nor writes
the table — but old code also does not CONSULT closures, so while it is running a
draft this build has permanently closed could be accepted by it. Prefer a forward
fix; if a rollback across this change is unavoidable, hold it.

### Query cost

`PUT orders/submit/` goes **12 → 14** on the success path: one closure read, and
ONE catalogue statement for the whole purchase (the single-statement contract
`catalogue_snapshot` already holds, so it is flat in the size of the order, not
per line). A REPLAY is unchanged at **7** — it returns at the evidence read,
before any of this. The create path is unchanged: the operational verdict is
decided from facts already carried on the admission verdict and the table row the
transaction already locks.

### Cutover — FRONTEND FIRST, the opposite of §15

**Merge and deploy the client before this backend, and the window between them
is inert rather than an outage.** That is the reverse of §15's order and the
reverse of the usual additions-go-backend-first rule, so it is worth stating
why each direction behaves the way it does.

**Frontend first is a no-op against this (pre-D06) backend.** The client reads
the deadline only when `order_details.quote_protocol >= 1`, which this backend
does not send, so it consults no deadline and never calls `retire-quote` — a
route that does not exist yet. The new refusal vocabulary fires only on codes
this backend never emits; the two it does emit (`quote_ref_stale`,
`legacy_pricing_version`) are classified REPRICE, which is the action the
hand-written branches they replaced already took. The interceptor's widened
forward (409, and the new route) reaches a handler that reads the sentence off
either shape.

**Backend first strands a diner, narrowly but completely.** A deployed client
handles exactly two refusal codes and falls through for everything else, so a
`quote_expired` or `purchase_needs_review` refusal surfaces the sentence with a
Retry — and Retry REPLAYS the same acceptance (D04's issued-command record is
still outstanding, because only `quote_ref_stale` settles it), which is refused
identically. `reserveIntent` answers `outstanding`, so that diner cannot start a
fresh checkout either. It needs a draft left open past the 30-minute lifetime,
or a catalogue edit inside the checkout window, so it is uncommon — and it has
no in-app escape, which is what makes the ordering worth respecting rather than
treating as a preference.

**§15 went the other way for a reason that does not apply here**: there the
client had to send a precondition the old client did not have, so an old writer
against a new server was a correctness problem. Here the client only has to
UNDERSTAND answers it may not receive yet.

### Frontend work required

1. Branch on the machine `reason` rather than the sentence, and treat the four
   transient codes and the terminal ones differently — a terminal refusal must
   lead to a re-price, and a transient one must not.
2. Render the deadline from `order_details.quote_policy.expires_at`, gated on
   `quote_protocol >= 1`. Treat an absent `quote_protocol` as 0 and promise
   nothing — in particular it does **not** mean quotes never expire.
3. Use `PUT orders/retire-quote/` before minting a replacement quote, rather than
   discarding the old one locally.
4. Add the new route to the diner capability header allowlist
   (`_security/diner-capability-contract.ts`) — the session header must ride it,
   and that file is method-exact, so it fails closed until it is added.

### 16a. `quote_protocol` is now 2, and a retired quote is readable back (G3a)

**ADDITIVE. No request shape changes and no existing key changes meaning.**

At level 1 a closure was published on ONE response — the refusal that created
it, which is the single thing a client can lose. The diner's own order read
published neither the level, the deadline nor the closure, so a quote retired
for `purchase_needs_review` INSIDE its window was invisible on every surface a
recovering client could reach, and its only remaining move was to attempt an
acceptance. That is exactly what `retire-quote` exists to avoid: when the quote
IS still good, the attempt SUCCEEDS, claims a table and sends food to a kitchen
in order to ask a question.

What is added, all of it optional to read:

* `GET orders/journey/order-details/` (BOTH selectors) now carries
  `quote_protocol`, `quote_policy` and `quote_closure` — the same constant and
  the same two projections the initiate response uses, never a second
  definition.
* `order_details.quote_closure` joins the initiate response too, because a D04
  REPLAY returns an order created earlier whose quote may have been retired
  since. `null` when nothing has been retired; the key is always present.
* `quote_closure` is bounded to `{closed_at, reason, quote_ref, policy_version}`
  — no actor, no amounts, no catalogue detail, no order contents.

**THE LEVEL RAISE IS SAFE FOR THE DEPLOYED CLIENT, and that was checked rather
than assumed:** `quote-transition.ts` gates with `level < REQUIRED_QUOTE_PROTOCOL`
(1), so a server reporting 2 still passes and the deployed client keeps
consulting the deadline exactly as it did. A client pinned to 1 gains nothing and
loses nothing — it was RIGHT that level 1 said nothing about reading a closure
back, which is why this is a NEW LEVEL and not a new meaning for the old one.

**THE DEADLINE AND THE CLOSURE ARE INDEPENDENT FACTS.** A quote retired because
the purchase changed is finished while its deadline has not passed, so
`quote_policy.status` legitimately reads `live` beside a closure. A client must
treat the CLOSURE as the answer to "may this still be accepted".

**IT COSTS NO QUERY** on either surface: the diner read folds the relation into
the order fetch (`select_related('acceptance', 'quote_closure')`) and the
initiate path's re-read replaced a plain `refresh_from_db`. That is a
CORRECTNESS rule before a cost one — under READ COMMITTED each statement takes
its own snapshot, so reading the order in one statement and the closure in
another publishes a correlated answer describing a moment that never existed.


### 16b. Creation re-asks the caller's authority under the lock (A1)

**NO REQUEST SHAPE CHANGES. One new refusal, on a request that was already
acting on authority it no longer held.**

`api/v2/orders/initiate/` resolves its caller in AUTOCOMMIT — a diner's table
session, or a staff caller's `tables` module gate — and the transaction that
writes the draft then WAITS for the admission advisory lock and the table row.
A QR regeneration, a membership deactivated, a role removed or a restaurant
leaving the portal-access states committing inside that wait revokes exactly the
authority the request is still acting on. The ACCEPTANCE boundary has re-asked
that question since G1b; creation asked neither half of it, so the draft was
written and a daily ticket number spent on it, and only the later acceptance
refused it.

Both channels are now carried to the boundary and re-asked there — the same
three-facts-and-no-credential records G1b introduced (`TableCapability`,
`StaffAuthority`). Nothing is taken from a request body: the capability's facts
come from the table the session resolved to, and the authority's restaurant is
cross-checked against the `Restaurant` row the create service itself loaded
before the module gate is re-consulted.

**A REPLAY IS AUTHORIZED BEFORE IT IS DISCLOSED, AND EXEMPT FROM NOTHING ELSE.**
The idempotent branch hands back an existing order and (since §16a) the closure
recorded against it, so authorization must still hold for that — a revoked
session may not read an acceptance any more than it may create one. It stays
exempt from every NEW-ORDER rule: a pause, menu-only ordering, an item that has
since sold out and a quote that has since expired all leave a replay untouched,
because refusing an order that was already created and acknowledged on the
strength of a rule about new work is the retroactive refusal D04 exists to stop.

**WHAT A CLIENT SEES.** The refusal is each channel's existing non-disclosing
404 — the diner capability channel's, and the endpoint's — so a revocation
landing mid-request is indistinguishable from a principal that never had access.
A client already handles both: a denied diner credential drives the rescan panel,
and a 404 on the staff path is the established answer. **No new code is
required.**

COST: unchanged on every create path. The one added query is a lock-free table
re-read, paid ONLY on a matched replay by a caller that presented a diner
capability (1 SELECT -> 2). A keyless caller, a staff caller and every new-order
path pay nothing: the new-order path re-uses the row it locks anyway, and the
capability half is skipped outright when no capability was presented.

**ONE CORRECTION TO THAT REFUSAL, AND IT IS THE OPPOSITE OF A NEW ONE.** The
lock-free replay read first shipped behind a bare `except Exception`, which
returned `None` for a database error as readily as for a missing row — and
`None` is read as a REVOCATION, so a dropped connection, a statement timeout or
a query defect answered with the capability channel's opaque 404. That is the
identical answer a killed session gets: a diner would be told their table
session was no longer valid, and we would see an authorization event, for an
outage. The handler now catches only what it is for (`Table.DoesNotExist`, and
the three a malformed key raises) and everything else propagates, so such a
failure surfaces as a 500 and reaches ordinary error handling. **A CLIENT NEEDS
NO CHANGE**: the 404 contract for a genuinely revoked or vanished table is
unchanged, and a 500 was always the honest answer for a database that is down.


### 16c. A replay disclosure asks whether the session still exists (A1b)

**NO REQUEST SHAPE CHANGES. One narrowed answer, on the two branches that
disclose an existing order without running any eligibility rule.**

§16b re-asked whether the CAPABILITY presented was still the current generation.
That is one half of a diner's session; the other half is whether the table is
still a place a diner can be. `_resolve_table` re-checks
`is_available_for_scan()` live on every ordinary use, so a soft-deleted,
disabled, deactivated or out-of-service table revokes the session at the door —
but both REPLAY branches return before any eligibility rule runs, so that fact
reached nothing inside them:

* `_create_order`'s idempotent replay hands back the existing order and the
  closure recorded against it — and it has **TWO** such returns, not one: the
  step-1 lookup before the locks, and the step-1c POST-WAIT RECHECK, which is
  how a request whose key was unused when it arrived is answered when a
  competing request carrying that key commits inside the wait. Step 1b'
  re-asks the capability's GENERATION on the locked row and the staff module
  gate; neither asks whether the table is still scannable, and going out of
  service bumps no `qr_version`;
* `_submit_order`'s accepted-submission replay hands back a 200 `idempotent`
  acceptance result.

Both now ask `diner_capability.session_still_admissible(capability, table_row)`
and answer the capability channel's own **opaque 404** when it is false. A caller
that presented NO capability is unaffected — a staff principal holds no table
session, so there is none for a table going out of service to revoke, and the
predicate returns True for them by definition.

**THE EXEMPTION IT KEEPS IS STILL THE RIGHT ONE.** A replay stays exempt from
every NEW-ORDER rule: a pause, menu-only ordering, an item that has since sold
out and a quote that has since expired all leave it untouched, because refusing
an order that was already created and acknowledged on the strength of a rule
about new work is the retroactive refusal D04 exists to stop. Table and
restaurant LIVENESS is not a new-order rule — a table that is not a place an
order can exist is not a place one can be read back either.

**WHERE THE DECISION LINEARIZES, stated rather than claimed away.** The
create-path re-read is deliberately lock-free and sits before the table lock, so
recovery never queues behind live ordering; under READ COMMITTED it takes its own
snapshot and therefore sees any COMMITTED revocation, which is the question. A
revocation committing a moment later can still overlap. The acceptance path asks
inside the locks it already holds.

**WHAT A CLIENT SEES.** The capability channel's existing non-disclosing 404,
which a deployed client already reads as a denied credential and answers with the
rescan panel. **No new code is required**, and the change is invisible to a
client whose table is still scannable.

COST: unchanged. The create path re-uses the single lock-free row read §16b
already pays for on a capability-carrying replay (memoised, so one statement
answers both questions); the post-wait return reads the row step 1b already
locked; the acceptance path reads the table row it has already locked.

**THE THIRD RETURN-EXISTING SITE IS DELIBERATELY UNCHANGED.** The
unique-conflict recovery sits AFTER the authoritative eligibility check, which
evaluates table liveness on the locked row for EVERY provenance, so an
unscannable table has already been refused there — with the diner-readable 400,
which is the right answer for work that is genuinely new. **The session question
is equally deliberately NOT asked beside the authority check**: doing so would
replace a FIRST creation's readable refusal with the opaque 404.

**ONE ORACLE MOVED, AND IT IS RECORDED RATHER THAN REWRITTEN.**
`test_a_table_taken_out_of_service_still_replays` asserted the OPPOSITE of this
rule, through a carried diner capability. It is replaced by
`test_THE_REGRESSION_an_unscannable_table_does_not_replay_to_a_diner`; the
keyless/internal control, the staff control and the paused-restaurant,
menu-only, stock-change and valid-rescan controls are all kept separately,
because an internal call with NO capability is not an oracle for a public request
carrying a revoked one.

### The refusal is the door's refusal, byte for byte

Every boundary above is documented as answering "the capability channel's OWN
opaque 404", and three of them answered something else. The door raises
`DinerCapabilityDenied` and both order endpoints render it as `exc.message` —
`'Not found.'` — while `_session_refusal` (create), the accepted-submission
replay and `retire_quote_for_review` each wrote out `'Not found'` by hand. One
route, two spellings, decided by WHEN the revocation landed.

**THE CLIENT-VISIBLE CONSEQUENCE IS LARGER THAN THE ORACLE.** As an oracle it
lets a caller separate a door refusal from a post-wait one, and liveness
revocation from generation revocation, in a channel built to disclose nothing.
But the deployed client matches the body EXACTLY
(`DinerSessionService.CAPABILITY_DENIED_404`, compared with `===` after a
`trim()` that does not strip a trailing period), so the periodless form was not
recognised as a capability denial at all: a diner whose table went out of
service mid-request was shown **no rescan panel**, and the client went on
treating a dead credential as live.

**`diner_capability.denial_envelope()` is now the one answer**, DERIVED from
`DinerCapabilityDenied` rather than re-spelled, so changing the channel's word
moves every site at once. All three sites use it — including
`retire_quote_for_review`, which this finding did not name; fixing two of three
would have left the same defect behind one door.

**NO WIRE SHAPE CHANGES.** The status is 404 and the keys are `status` and
`message` on every path, before and after. What changes is that three paths now
emit the same `message` the door has always emitted, which is what the contract
already said they emitted.

**THE STAFF CHANNEL IS DELIBERATELY UNTOUCHED.** Its door is the orders
endpoints' own periodless `'Not found'`, and `StaffAuthorityError` already
matches it; routing staff through the diner envelope would introduce there
exactly the mismatch this removes here. A control pins that the two channels
stay different.


## 17. In-app subscription collection is refused — `POST finances/transactions/` now answers 501 (D07)

**What changed.** `POST /api/v1/finances/transactions/` with
`{"transaction_type": "subscription", ...}` used to answer **200** with
`{"status": 200, "message": "The subscription payment has been initiated. Please
confirm payment when promted"}` and persist a Pending `DinifyTransaction`. It now
answers:

```
HTTP 501
{"status": 501,
 "reason": "subscription_collection_unavailable",
 "message": "In-app subscription payment collection is not available. This request
             did not create or send a payment request."}
```

**Why.** There has never been a collector behind that sentence. The provider call
was a comment; `notifications_app.controllers.sms` holds the only outbound HTTP
call site in the tree, and the repository carries no PSP credentials and no
payment-execution code. A 200 saying a payment "has been initiated" is a claim
about work no provider performed, and the Pending row it left behind was evidence
of a payment attempt that never happened.

**501, not 503, and no `Retry-After`.** RFC 9110 §15.6.2 is "the server does not
support the functionality required"; §15.6.4 is a temporary overload or
maintenance. A `Retry-After`, a retry timer or a client-side poll would each
suggest that waiting implements a collector. Nothing here suggests that.

**What is unchanged.** The endpoint's authorization runs FIRST and is untouched:
`can_manage_restaurant` resolved against the body's `restaurant_id`, answering
**404** (never 403) so a non-member cannot learn whether a restaurant exists, and
failing closed on a missing or empty id. The transaction-type dispatch keeps its
existing 400 for an unknown type. An unknown or malformed `restaurant_id` still
reaches the same opaque 404 rather than a 500. Historical `DinifyTransaction`
rows, the model, its serializers and both Transactions reports are untouched —
this changes what the server will DO, never what it has recorded.

**The legacy plan column no longer decides anything.**
`preferred_subscription_method == 'per_order'` used to be the one branch above the
200 path. There is no 200 path now, so branching on it would offer changing a plan
as a way to enable machinery that does not exist; every authorized request gets
the same answer whatever the column says. (It was never writable from the customer
plane anyway — `restaurant_setup.py` strips it, with `flat_fee`, from every
`restaurants` PUT, and that strip is unchanged.)

**The refusal is in the SERVICE, not the view.**
`finance_app.controllers.tx_subscription.initiate` is reachable in-process by any
caller — including one passing `user=None`, which used to persist
`created_by=None` — so disabling a view or hiding a button would have left the
fake collection fully available. Every caller reaches the refusal, and the
insertion branch is REMOVED rather than parked behind an enable switch: a switch
is a working fake collection one boolean away from returning.

### CUTOVER — FRONTEND FIRST, and the window is a real (small) regression

Ship the restaurant portal's read-only billing screen BEFORE this. The paired
frontend removes the Pay/Renew/Subscribe control entirely, so against either
backend it sends no `finances/transactions/` request at all — inert.

Backend first is NOT inert. An older deployed client still runs its pre-submit
chain before it ever reaches this refusal: a `users/msisdn-lookup/` probe and a
**REAL OTP** through `users/auth/resend-otp/`, for a payment that then fails. The
operator gets a verification code by SMS and a failure — worse than the honest
refusal, and completely avoidable by ordering. Nothing is lost either way: no
payment was ever collected on either side of this change.

### ROLLBACK

Reverting restores the 200 and the Pending row — that is, it restores the payment
claim. Prefer a forward fix. A rollback across this is a deliberate decision to
re-enable a message the platform cannot honour, not a neutral revert, and it is
schema-free: no migration accompanies it, so the databases match either way.

---

## 18. Historical order lines carry the name they were bought under, and say when it is missing (D12, reader)

**What changed.** Six diner-facing name fields used to read the LIVE catalogue
record (`MenuItem.name`), or fell back to it. They now return the line's saved
`OrderItem.item_name_snapshot` VERBATIM, and each gains an additive sibling that
says where the name came from:

| Existing name field | New sibling |
|---|---|
| `GET orders/journey/order-details/` → `data.items[].item.name` | `data.items[].item.name_provenance` |
| same → `data.items[].extra_items[].name` | `data.items[].extra_items[].name_provenance` |
| `POST orders/initiate/` (including a D04 replay) → every `serialize_order_item_details` row's `item_name` (`order_items`, `available_items`, `unavailable_items`, `extras`, `available_extras`, `unavailable_extras`) | `item_name_provenance` |
| same rows → nested `extras[].item_name` | `item_name_provenance` |
| `quote[].item_name` (initiate and order-details) | `item_name_provenance` |
| `quote[].extras[].item_name` | `item_name_provenance` |

`*_provenance` is `"snapshot"` when the saved name is non-empty and `"missing"`
when it is `""`. It describes THAT NAME FIELD ONLY, and says nothing about the
line's money, options or allergens.

**Why.** A catalogue record keeps changing after the purchase. A rename moved
three of these fields to the new name. A soft delete's inline vacuum rewrote the
record to `<name>_autodelN`, and that string reached the diner as the dish they
ordered. The quote already preferred the saved name, but when the saved name was
blank it silently substituted today's catalogue name as though it were history.

**Before / after**, for a line bought as "Beef Burger" with a "Cheese Slice"
extra, after the operator renamed the extra "Vegan Cheese" and then deleted the
dish (whose record the inline vacuum rewrote to `Beef Burger_autodel1`):

```
before  items[0].item            {"id": "…", "name": "Beef Burger_autodel1", "is_special": false}
        items[0].extra_items[0]  {"id": "…", "name": "Vegan Cheese", …}
after   items[0].item            {"id": "…", "name": "Beef Burger",
                                  "name_provenance": "snapshot", "is_special": false}
        items[0].extra_items[0]  {"id": "…", "name": "Cheese Slice",
                                  "name_provenance": "snapshot", …}
```

and for a line whose saved name is blank:

```
before  quote[0]  {"item_name": "Chicken Burger", …}          # today's name, unlabelled
after   quote[0]  {"item_name": "", "item_name_provenance": "missing", …}
```

**A blank is reported, never filled.** `""` stays `""`, a string, never `null`
and never a substituted current name. Nothing is trimmed, suffix-stripped or
normalised. A saved name that happens to contain `_autodel` is kept intact,
because it is what the diner saw. **What a blank does NOT tell you:** that the row
predates migration `orders_app/0028` (which added the column with no backfill), or
that the order carries no intent key. The tests include an intent-bearing blank
row read through a D04 replay and through `?intent=`. The server does not know why
a name is missing, and it does not guess.

**What is unchanged.** Money: every legacy numeric key keeps its form, and every
quote amount stays an exact decimal string. Identity: `id`, `item`,
`items[].item.id`, `is_special`, `selected_modifiers`, `modifiers`, `options`
and allergens are unchanged. Also unchanged: availability, status and deletion
fields, row order, the population of every list (and which extras sit under which
parent), grouping, `quote_total`, `quote_complete`, `quote_ref`, and the
acceptance and closure projections. **`quote_ref` does not move**: it fingerprints
the SAVED columns, never the reader's output. The kitchen feeds are unchanged
(they already read the snapshot), and so is the menu-performance report, which is
a current-menu report by contract. The detail read costs two fewer queries in
the tested fixture (the extras no longer load their `MenuItem` to read a live
name). The query population was not otherwise touched.

**No migration, no backfill, no data write.** A backfill was considered and
refused. The snapshot columns are inside the quote fingerprint, so rewriting a
DRAFT's saved name moves its `quote_ref` and its next submit answers
`quote_ref_stale`. That is a re-review, not a terminal closure, but it is still
a change to what the diner agreed to. And no trustworthy source for a
historically correct name was identified: the live catalogue is exactly the
wrong source. That is not proof that no archive exists anywhere.

### Frontend consumers (Dinify-Frontend `0ea7e9b`)

The basket review sheet reads `quote[].item_name` and `quote[].extras[].item_name`,
and those values change for blank rows. A probe ran the pinned
`quote-review.ts` / `quote-equivalence.ts` against real wire payloads from this
branch:

- normal names are readable and pair with the basket (the plain prompt stays
  available);
- an equal-total basket whose two line prices moved by offsetting amounts is still
  refused (control);
- a blank parent name, a blank extra name, or both are READABLE (`""` passes
  `displayableName`), but no longer pair with a basket that knows the name. So
  the diner gets the itemised review, which shows a blank name, instead of the
  plain prompt;
- blank against an equally blank basket name pairs (recorded, not asserted).

Against the pre-change reader the same blank rows were filled with the live name
and paired. `""` parses everywhere, and nothing at `0ea7e9b` DISPLAYS the new
provenance fields: a blank renders as an empty name, not as "name unavailable".
No Frontend change is required for this to be safe, and none is made here. The
kitchen board is untouched, and its wire validator still rejects a `null` name,
which is why the blank stays `""`. **Consumers outside the three repositories
are unknown.** Any that treated `items[].item.name` or the initiate `item_name`s
as the CURRENT catalogue name will now see the purchase name.

### What this does NOT close (D12 stays open)

- **Hard deletion.** `OrderItem.item` is still `on_delete=CASCADE`, so a hard
  delete of a purchased `MenuItem` destroys the order's lines, and an orphaned
  extra (`parent_item` is SET_NULL) then reads as a main dish. PROTECT is a
  proposal only. A rollback could restore CASCADE, and a waiver is not a repair.
- **The kitchen gap.** A blank legacy row shows `""` and `allergen_tags: []` on
  the kitchen board. An unknown allergen list is NOT "no allergens", and the
  kitchen serializer is unchanged here.
- **Descriptions and other catalogue text.** There is no description snapshot,
  and this change adds none.
- **Retention.** Nothing here decides how long saved names are kept, or recovers
  a name that was never saved.

---

## 19. Admin commands can name the session they were issued under (D10, B1)

**What changed.** Two additive response fields, one optional request header and three
new refusals, all on the admin control plane (`admin.dinifyapp.com`; browser paths are
`/api/admin/v1/...`, reached as `admin/v1/...` after Apache strips `/api`). No
migration, no settings change, no new route and no CORS change (the plane is
same-origin).

`POST /api/admin/v1/auth/verify/` and `GET /api/admin/v1/auth/session/` add
`data.command_owner`:

```
"command_owner": {"version": 1, "actor": "<User.pk>", "session": "<AdminSession.id>"}
```

`verify/` names the session it has just issued (the one its `Set-Cookie` carries).
`session/` names the session that authenticated that read. Both ids are canonical
lowercase UUID strings. They identify and grant nothing: neither is the session token
or its hash. Every existing field, the envelope and every cookie are unchanged.

A client may name that owner on any unsafe admin request:

```
X-Admin-Command-Owner: 1;<actor>;<session>
```

| Header | Result |
|---|---|
| absent | unchanged: the pre-D10 contract |
| names the session that authenticated the request | unchanged: CSRF, permissions and elevation still apply |
| present, but not exactly one well-formed value | `400 {"detail": "…", "code": "admin_command_owner_malformed"}` |
| names a different administrator | `409 {"detail": "…", "code": "admin_command_actor_changed"}` |
| same administrator, a different session | `409 {"detail": "…", "code": "admin_command_session_changed"}` |

The check runs in `AdminSessionAuthentication`, in this order: the session cookie is
resolved (a missing, unknown, expired, idle or revoked session is still the existing
`401`); the account's eligibility is re-checked (still the existing `401`); then the
owner; then CSRF (`403`); then permissions, including recent elevation (`403`);
throttles; the handler. So an owner refusal is never reported as a CSRF failure, and
it happens before any second factor is checked or spent. Safe methods ignore the header
entirely. Each refusal carries one fixed sentence and its code, never an id, the header
or a cookie value, and is not audited: like a CSRF failure it is refused inside
authentication, before any administrative decision exists.

**The session comparison is what enforces.** The actor comparison only chooses which
409 is returned: "somebody else is signed in now" and "you signed in again" need
different screens.

**Parsing is strict, and only absence is legacy.** The value must be exactly `1;`
followed by two lowercase, hyphenated UUIDs joined by `;` (75 characters). An empty
value, whitespace, a missing or extra field, another version, an uppercase, braced or
`urn:` id, surrounding whitespace and two values joined by a proxy (`a, b`) are all
`400`. Nothing is trimmed or lower-cased.

`POST /api/admin/v1/auth/logout/` does not use the authenticator, so it applies the same
rules itself:

| Header | Live session behind the cookie | Result |
|---|---|---|
| absent | any | unchanged: revoke it if live, clear both cookies, one audit entry |
| malformed | any | `400`; nothing revoked, no cookie cleared, no audit |
| well-formed | none (no, unknown, expired, idle or revoked cookie) | `200 {"status": 200, "message": "Signed out."}` with NO `Set-Cookie`; nothing revoked, no audit |
| well-formed, another session | live | `409` as above; no `Set-Cookie`, nothing revoked, no audit |
| names this session | live | unchanged |

**Why.** A matched CSRF pair says a request came from this origin. It does not say
which session a command was issued under. `session/` calls `get_token`, which
re-emits whatever CSRF secret the request carried. So a `session/` response sent
before another sign-in and delivered after it puts the old CSRF cookie back beside the
new session cookie. A tab still holding the old token then passes CSRF, and its command
runs as whoever signed in since. A second sign-in by the SAME administrator has the same
shape, with nothing about CSRF stale at all.

**This corrects §12, item 3.** It said the rotated token "is bound to the
`AdminSession`, the way `django.contrib.auth.login()` binds it". It is not. Rotation
gives each sign-in a fresh secret, but nothing records which secret belongs to which
session, `logout/` leaves the CSRF cookie in place, and `session/` re-emits an old one.
§12's advice for a CSRF `403` (re-bootstrap with `session/` and retry once) is still
right for a genuine CSRF failure. A client that names its owner never reaches it after a
session change, because the `409` comes first.

**Frontend action (Dinify-Admin).** None to keep working: an absent header is the old
contract, and the deployed client sends none. To be protected, a client must:

1. keep the `command_owner` it was established under, from `verify/` or `session/`;
2. send it on every unsafe request except `login/` and `verify/`, unchanged on the one
   CSRF retry and on the replay after re-authentication;
3. treat both `409` codes as "the command was not run", and never retry, elevate or
   re-bootstrap and retry after one;
4. treat `400 admin_command_owner_malformed` as a client defect;
5. name its owner on `logout/`, so a stale tab cannot sign out a later session.

A client that consumes this contract must not ship before this change is merged and
deployed.

**What this does NOT close.**

- **A client that sends no header is still exposed.** The deployed Admin client sends
  none, so the counterexample stays open for it until a client that names its owner is
  deployed.
- **A delayed MATCHING sign-out** still clears the cookie of a session started after it
  was sent, because its response cannot know what the browser holds by the time it
  lands. The later session's row is untouched and nothing runs; that browser has to
  sign in again.
- **An outcome that was already uncertain stays uncertain.** A command sent under the
  right owner whose response is lost may or may not have run, exactly as before.
- **The serving path must carry the header, and this repository cannot show that it
  does.** A proxy that strips `X-Admin-Command-Owner` turns every request into the
  absent-header case: nothing fails, and the protection is silently gone. A `GET` of
  `session/` cannot detect that. Before a client relies on the header, an
  owner-authorized negative probe on controlled, disposable fixtures must show the
  deployed path returning the `409`.
- **Rolling this back is not neutral.** A backend that ignores the header runs every
  command a new client sends, with no error. Keep this contract when reverting a client:
  reverting client files does not replace tabs that are already open.

---


## 20. OTP issuances and wrong codes are recorded as evidence, and `make_otp` refuses an enclosing transaction (D11 B2-C)

**This is evidence collection, not enforcement.** No rate-limit, shadow, admission or
refusal decision reads these rows, and D11 stays open for policy, enforcement and
operational acceptance. No request shape, response shape, status code, message or
cookie changes.

**Schema.** Migration `users_app/0015_otp_accounting` follows `0014` and adds two
tables, `otp_issuances` and `otp_verification_failures`. It is expand-only: two
`CreateModel`s, no backfill, no `RunPython`, no existing table altered. The keys are
pseudonymous, not anonymous (`u:<User UUID>`, or a versioned HMAC of the canonical
phone under the existing OTP pepper; the OTP hash itself is not stored). A pepper
rotation would change phone keys and split historical grouping; nothing rotates or
reconciles them. See `docs/engineering/d11-b2-collection.md`.

**Internal contract changes a caller can notice.**

- `OtpManager.make_otp` gains an optional `origin` keyword. The production callers
  name theirs; any other caller is recorded as `unattributed`.
- `make_otp` now writes the replacement delete, the new challenge and its ledger row in
  one `transaction.atomic(durable=True)`, taking the user's row `FOR KEY SHARE` first
  (user-backed challenges only). **Calling it inside a caller's transaction raises
  `RuntimeError`** before any write or send; no production caller does. A test wrapped
  in `TestCase` is unaffected (Django exempts its own wrappers). The transaction
  commits before any sender runs, and nothing is held across transport.
- If the ledger row cannot be written, the challenge rolls back, nothing is sent and
  `make_otp` returns `False`, which every caller already treats as a delivery failure.
- An issuance's `accepted` state means the existing sender REPORTED acceptance, not
  that a handset received the code. The dev OTP `1234` is unchanged.
- A wrong code is observed in a savepoint after the attempt counter is saved; if the
  observation fails, the counters (and an owner claim's `claim_failed_attempts`) still
  commit with the ordinary refusal. A lost connection is re-raised.
- `manage.py prune_otp_accounting` (`--batch-size` 1–1000, default 500;
  `--max-batches` 1–10000, default 100; no retention override; not scheduled). It
  prints committed counts per batch and in total, refuses to run inside a caller's
  transaction, says `complete` only after a fresh bounded check, and on failure exits 1
  with the confirmed counts, a fixed category and the failed statement's outcome called
  unknown — never database text.

**Rollback.** Code without this change neither reads nor writes the tables, so a
rollback stops collection and automatic cleanup and leaves the tables and their rows in
place. No reverse migration and no deletion is authorized; removing collected rows
later needs an operator to run cleanup under separate authority. Claude-reported and
local only: before #352 merged, the users_app, notifications_app and owner-claim suites
of the then-current `origin/main` (639 tests) passed against a database migrated with
`0015`. That run did not record its commit; `0513adb` is inferred from the chronology
and is not a demonstrated tested revision.

**Deployment, as the workflow recorded it.** The legacy UAT deploy of #352 (run
36568672986) reports target `ef376b2`, `Applying users_app.0015_otp_accounting... OK`,
the routing probe at HTTP 405 and the database probe at HTTP 200 / `connected`, around
12:32 UTC on 2026-09-29. The #353 deploy (run 36573040494) reports `ef302f3` and `No
migrations to apply`. These are workflow observations: they do not show a collected
row, a first-record time or a verified runtime identity. Collection happens only when
the new code runs an OTP path against the migrated schema. Seven days is when a row
becomes ELIGIBLE for deletion, not a guaranteed maximum age: rows go only when the
opportunistic batch or the command runs.

---


## 21. Password-reset initiation answers one acknowledgement, and completion is bound to a reset challenge (D11 E-R1)

**Response contract changes (both reset routes).**

- `POST users/auth/initiate-reset-password/`, and `POST users/auth/reset-password/`
  with no `otp`: every acknowledged identity — eligible and issued, unknown, platform
  staff, `pending_initial_claim`, and an email several accounts share exactly — answers
  `200 {"status": 200, "message": "If these details match an eligible account, check its
  registered phone or email for a reset code."}`. **`data.user_id` is REMOVED.** The
  refusals used to be `400 NO_PHONE_NUMBER`; the eligible answer used to carry
  `user_id` and "An OTP has been sent…". An exactly shared email used to be a 500.
- **Unchanged:** the `NO_RESET_IDENTIFIER` 400 for a request naming no one; the
  identifier-over-`phone_number` precedence; the throttle; and the issuance FAILURE
  responses — a `make_otp` that cannot record or send answers the same 500 as before,
  and is never acknowledged.
- `POST users/auth/reset-password/` with an `otp`: every failure — unknown, refused or
  ambiguous identity, wrong code, no live reset challenge — answers
  `400 {"status": 400, "message": "Invalid OTP."}` (an unknown or refused identity used
  to answer `NO_PHONE_NUMBER`). The success body is unchanged.

**Behaviour change.** Completion verifies with `expected_purpose='reset-password'` and
no destination binding. A login or owner-claim challenge for the same account is no
longer selected, charged or consumed by a reset, and its code no longer completes one.

**Consumer.** The deployed Frontend ignores the initiation body, so the removed
`user_id` breaks nothing there; its screens said "We sent a one-time code…", which is
false for a request that matched no one. **CUTOVER: FRONTEND FIRST** (the conditional
wording PR), confirm its deployment, then this Backend change. Backend first is not
unsafe — every request still works — it only leaves the old wording claiming a send
that may not have happened, for the width of the window.

**No migration.** A rollback restores the previous answers, and with them the
disclosures this removed.

**Not closed.** The 500 and response timing still distinguish an eligible identity (the
500 permanently, for an account with no sendable destination); `resend-otp` with
`purpose='reset-password'` still answers absent, pending and eligible accounts
differently; an anonymous initiation can still replace an in-flight login challenge;
registration and abuse limits are unchanged. D11 remains open.

---

## 22. OTP challenges are replaced only within their purpose, `verify-otp` is login-only, and generic resend takes four purposes (D11 E-R2)

**Three changes, one deployable unit.** None is safe alone: with only the purpose-scoped
replacement, a reset challenge now coexisting with a login challenge would hide the
login code at an unbound `verify-otp`; and the replacement cannot stop a resend that
names `owner-claim`, which lands in the claim challenge's own bucket.

**Internal behaviour change, no request or response shape (P1).**
`OtpManager.make_otp` replaces only an earlier challenge with the same
`(user, msisdn, purpose)` (`purpose=None` matches only null). A password login and an
anonymous reset initiation, which both store `msisdn IS NULL`, no longer delete each
other's live challenge, and an account-resolving resend no longer deletes the owner's
claim challenge. That deletion used to make the owner's correct code fail and charge
the invitation's `claim_failed_attempts`; five such cycles reached
`verification_locked`. A second request for the same purpose still replaces the first.
Lock order, accounting, the 5-minute expiry, the 5-attempt cap, single use, sender
ordering and the dev OTP `1234` are unchanged.

**Response contract change: `POST users/auth/verify-otp/` (P2).** It verifies LOGIN
challenges only (`expected_purpose='login'`, no destination binding). A reset,
owner-claim, `register` or null-purpose code submitted there now answers
`200 {"status": 200, "message": "Invalid OTP", "data": {"valid": false}}`, and that
challenge is not selected, charged, observed or consumed. The submission is compared
against the user's newest live login challenge, if there is one, and counts as a wrong
code against it. Before, the route took the newest live challenge of ANY purpose: a
correct reset or claim code answered `{"valid": true}` with no tokens and was used up,
so its own flow then failed, and a wrong guess was charged to it. A login code still
verifies, whether it came from password login or a login resend, and the existing
pending and platform-staff gates on minting are unchanged.

**Response contract change: `POST users/auth/resend-otp/` (P3).** `purpose` must be
exactly `"login"`, `"reset-password"`, `"register"`, `null`, or omitted. Any other
value — `"owner-claim"`, `"first-time-payment"`, an unknown, empty, differently cased or
padded string, a number, a boolean, an array or an object — answers
`400 {"status": 400, "message": "Invalid purpose"}`. This is decided before any
account lookup and before the identification/identifier presence check, so the answer
is the same for a known, unknown or missing identifier and for a signed-in caller.
Nothing is issued, deleted, recorded or sent. Previously every value issued a challenge
(or got its account-specific answer). An owner-claim code now comes only from
`users/owner-claim/challenge/`. The allowed purposes keep their existing gates: the
5-minute password anchor for `login`, and the customer-access and reset answers for
`reset-password`.

**Consumers.** Traced in Dinify-Frontend `0cdffe2`: its login screen calls `verify-otp`
with `{user, otp}` and resends with `purpose: "login"`; its register and profile screens
resend with `purpose: null`; forgot-password uses its own two routes; owner claim uses
its own routes. None of them sends a refused purpose or verifies a non-login code at
`verify-otp`, so no Frontend change is needed and there is no cutover order. This covers
the traced supported clients only: an external caller that verified a non-login code at
`verify-otp`, or resent under another purpose, now gets `valid: false` or the 400.
Registration verifies through its own route and stays purpose-unbound. The profile
screen's code is never verified, and the Backend refuses a phone change anyway.

**Concurrency, as observed.** On PostgreSQL, a correct login verification and a reset
initiation no longer wait for each other (before, the initiation's DELETE locked the
login row). An owner's claim redemption and a reset resend to the same phone still
serialize on the owner's `users` row, in both arrival orders, and then both succeed with
the claim challenge intact. These are the schedules that were run, not a proof that no
interleaving can deadlock.

**No migration. Rollback** is a code rollback that restores nothing. Challenges, ledger
rows and `claim_failed_attempts` written under this change stay as written, and the old
build's cross-purpose replacement and any-purpose selection apply again, including to
challenges still live at the time.

**Not closed.** Registration verification is purpose-unbound and its msisdn lookup does
not require `user IS NULL`. A password reset, initiated or completed, does not revoke an
outstanding login challenge: the resend path already let one survive, this extends that
to ordinary initiation, and coordinated invalidation is an open policy. Two concurrent
requests for the same purpose can still leave two live challenges (an existing race; no
uniqueness constraint was added, by scope). Allowed resend purposes still answer per
account (the §21 residual). Requester-bound resend, numerical abuse budgets and their
enforcement are undecided. D11 remains open.

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
10. **Historical line names (D12):** none required. Order-line names are the
    saved purchase name, and a missing one is `""` beside an additive
    `*_provenance: "missing"`. Optionally render that as "name unavailable" — see §18.

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
