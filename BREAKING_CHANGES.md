# Backend Stabilization — Frontend-Impacting Changes

This document lists all API contract changes introduced by the backend
stabilization work.  **The frontend must be updated to match before merging
this branch into production.**

---

## 1. Login endpoint — OTP-required response no longer includes tokens

**Endpoint:** `POST /users/auth/login/`

**Affected users:** Privileged roles (restaurant owners, managers, finance,
Dinify admins/superusers) when `source != 'diner'`.

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

## Summary of frontend changes needed before merge

1. **Login flow:** Stop reading `token`/`refresh` when `require_otp == true`.
   Get tokens from `verify-otp` instead.
2. **Password reset:** Implement two-step OTP flow using
   `initiate-reset-password` + `reset-password` + `change-password`.
3. **Error handling:** Handle `4xx` HTTP status codes (especially `401`,
   `403`, `429`) instead of assuming all responses are `200`.
4. **Token refresh:** Optionally use `/users/auth/token/refresh/` for session
   extension.

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
- `OrderPaymentsEndpoint` uses `AllowAny` — intentional for anonymous web
  payments but should be reviewed for whether unauthenticated users should be
  able to initiate payments.
- `MsisdnLookupEndpoint` uses `AllowAny` — may allow user enumeration by
  phone number. Needs product decision.
