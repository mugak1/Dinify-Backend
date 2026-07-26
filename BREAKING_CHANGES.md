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
