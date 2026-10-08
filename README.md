# Dinify Backend

Django REST API backend for the Dinify restaurant management and ordering platform.

## Tech Stack

| Component | Version / Package |
|---|---|
| Python | **3.12.3** — CI pins the exact patch the prod EC2 runtime serves (Ubuntu 24.04); 3.12+ locally |
| Django | 5.2.15 (LTS) |
| Django REST Framework | 3.17.1 |
| Auth | `djangorestframework-simplejwt` 5.5.1 (JWT Bearer tokens) |
| Database (primary) | PostgreSQL via `psycopg` 3.1.18 |
| Database (document store) | MongoDB via `pymongo` 4.18.2 |
| HTTP client | `requests` 2.34.2 |
| Image handling | `Pillow` 12.3.0 |
| CORS | `django-cors-headers` 4.9.0 |
| Config | `python-decouple` 3.8 |

Full dependency list: [`requirements.txt`](requirements.txt)

## Local Setup

```bash
# 1. Clone the repository
git clone <repo-url> && cd dinify_backend_handover

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env — at minimum fill in DATABASE_PASSWORD, EMAIL_HOST, and EMAIL_PASSWORD

# 5. Ensure PostgreSQL is running and create the database
createdb dinify   # or via psql: CREATE DATABASE dinify;

# 6. Run migrations
python manage.py migrate

# 7. Create a superuser
python manage.py createsuperuser

# 8. Start the development server
python manage.py runserver
```

**Note:** Some features (notifications, action logs, record archiving) require a running MongoDB instance. The connection is configured via `MONGO_HOST` and `MONGO_DATABASE` environment variables — see `dinify_backend/mongo_db.py`. The `.env.example` file does not currently include these variables; you will need to add them manually if you use MongoDB-dependent features.

## Architecture Overview

The project uses a single Django settings file (`dinify_backend/settings.py`) with one PostgreSQL database. MongoDB is used as a secondary document store for notifications, action logs, and record archiving.

### Installed Django Apps

| App | Purpose |
|---|---|
| `users_app` | Custom user model (`AUTH_USER_MODEL`), authentication (login, OTP verification, password reset), user profile management, and role-based access. Defines the `BaseModel` abstract class used by most other models (with audit fields and soft-delete). |
| `restaurants_app` | Restaurant CRUD, employee management, menu structure (sections, groups, items with options and extras), dining areas, and tables. Also handles restaurant subscription configuration. |
| `orders_app` | Order creation, item-level status tracking, pricing/discount calculations, customer assignment, and order ratings/reviews. |
| `finance_app` | Record-only transaction history: the `DinifyTransaction` model, its serializers, and the subscription writer (`tx_subscription.py`, reached via `TransactionsEndpoint`). Holds no balances and executes no payments — see [`REGULATORY_AUDIT.md`](REGULATORY_AUDIT.md). |
| `notifications_app` | Email and SMS dispatch. Reads unsent notifications from MongoDB and sends them. Has no Django models. |
| `reports_app` | Restaurant report generation (dashboard, sales, transactions, diners, menu). Has no Django models currently — report logic operates on other apps' data. |
| `support_app` | Restaurant-facing support ticketing (`SupportIssue`, collision-safe `SUP-000123` references). Secretary-pattern endpoints at `api/v1/support/`. |
| `reviews_app` | Visit-level diner reviews (one `Review` per `Order`) with dimension ratings, quick-chip tags, and owner/manager analytics. Endpoints at `api/v1/reviews/`. |
| `platform_admin_app` | Platform-staff control plane — a separate Django plane with its own settings, urlconf, and WSGI entry, served on `admin.dinifyapp.com`. TOTP-backed admin auth, append-only audit log, and time-boxed delegated access to a single restaurant. |
| `misc_app` | System-level configuration via `SysActivityConfig` model (boolean/integer/string/date settings). Also houses soft-delete vacuum utilities. |

### Third-Party Django Apps

- `corsheaders` — CORS header management
- `rest_framework` — Django REST Framework
- `rest_framework_simplejwt` — JWT authentication

### App Directory Convention

Each app generally follows this structure:
```
<app_name>/
├── controllers/    # Business logic
├── endpoints/      # API view classes and URL routing
├── management/     # Custom management commands
├── models.py       # Django models
├── serializers.py  # DRF serializers
└── tests.py        # Tests (where they exist)
```

## Environment Variables

Configured via `.env` file using `python-decouple`. See [`.env.example`](.env.example) for the template.

| Group | Variables | Purpose |
|---|---|---|
| Django core | `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `ENV` | App secret, debug mode, allowed hosts, environment identifier |
| Diner capability | `DINER_CAP_KEY`, `DINER_SESSION_TTL_SECONDS` | Signing key for anonymous diner QR credentials + table sessions (**required in production**, ≥32 chars, must differ from `SECRET_KEY`) and the session TTL (default 6h) |
| Platform admin | `ADMIN_SECRET_ENCRYPTION_KEY` | Fernet key encrypting platform-staff TOTP secrets at rest (**required wherever the admin control plane is used** — without it no admin can be created and TOTP cannot be verified; recovery-code sign-in still works, which is the documented way back in — see the break-glass sequence in [`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md). The customer API is unaffected) |
| CORS | `CORS_ORIGIN_ALLOW_ALL`, `CORS_ALLOWED_ORIGINS` | Cross-origin request policy |
| JWT | `JWT_ACCESS_LIFETIME_MINUTES`, `JWT_REFRESH_LIFETIME_DAYS` | Token expiry configuration |
| Database | `DATABASE_ENGINE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT` | PostgreSQL connection |
| Email | `EMAIL_HOST`, `EMAIL_ACCOUNT`, `EMAIL_PASSWORD`, `EMAIL_PORT` | SMTP email dispatch |

**Not in `.env.example` but referenced in code (via `test_settings.py` and integration modules):**

| Group | Variables | Purpose |
|---|---|---|
| MongoDB | `MONGO_HOST`, `MONGO_DATABASE` | MongoDB connection for notifications, callbacks, and logs |
| Yo Uganda | `YO_SMS_ACCOUNT_NO`, `YO_SMS_PASSWORD` | Yo SMS gateway (OTP + notifications) |
| Rate limiting | `THROTTLE_AUTH_LOGIN`, `THROTTLE_AUTH_OTP`, `THROTTLE_AUTH_OTP_IDENTIFIER`, `THROTTLE_AUTH_RESET` | Customer auth endpoint throttle rates (defaults: 10/min, 5/min, 10/min, 5/min) |
| Rate limiting (admin) | `THROTTLE_ADMIN_LOGIN`, `THROTTLE_ADMIN_LOGIN_IDENTIFIER` | Admin auth throttle rates (both default 10/min). Defence in depth only — the durable lockout is DB-backed on `PlatformStaffAuth` |

## Migration Workflow

The project uses a single default database (PostgreSQL). Standard Django migration commands apply:

```bash
# Create migrations after model changes
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Check for missing migrations
python manage.py makemigrations --check
```

There is no multi-database router configuration — all models use the `default` database.

## Management Commands

Full operations runbook — what each command touches, its idempotency and error
handling, and where the operational gaps are: [`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md).

`finance_app` has no management commands. The seven it previously shipped
(`check_dpo_transactions`, `verify-dpo-tokens`, `check_transaction_statuses`,
`check_yo_transactions`, `process_transactions`, `createaccountswithyo`,
`seed_dinify_account`) were removed in the custodial teardown.

### orders_app

| Command | Description |
|---|---|
| `determine-customers` | Matches orders with no assigned customer to existing users by phone/email, or creates new user records, then links the customer to the order. |

### notifications_app

| Command | Description |
|---|---|
| `send_messages` | Reads unsent notifications from MongoDB and sends them as emails (and optionally SMS for credential notifications), then marks each as sent. |
| `send_test_sms` | Sends ONE test SMS through the consolidated Yo sender and prints the raw gateway response. Bypasses the `ENV` gate on purpose, so gateway credentials can be checked without moving off `ENV=dev`. Target: `--to <msisdn>`, else `TEST_SMS_RECIPIENT`. |

### restaurants_app

| Command | Description |
|---|---|
| `optimize_images` | Optimizes all existing menu item, restaurant, and section images. |
| `reoptimise_menu_images` | Re-optimises existing `MenuItem` images, converting them to WebP at the current `optimize_image()` defaults. Safe to run repeatedly. `--dry-run`, `--limit N`. |
| `check_item_data` | Debugging helper — inspects `MenuItem` fields (options, allergens, flags). `--name`, and `--clean-allergens` to strip empty/whitespace entries from the allergens list. |

### platform_admin_app

Operator commands, run by hand over SSH — not scheduled tasks. See
[`BACKGROUND_TASKS.md`](BACKGROUND_TASKS.md) for the full runbook, including the
break-glass sequence for a lost `ADMIN_SECRET_ENCRYPTION_KEY`.

| Command | Description |
|---|---|
| `create_platform_admin` | Creates a `platform_staff` account for the admin control plane: prompts for the password interactively, enrols TOTP, and prints the `otpauth://` URI, an ASCII QR, and ten one-time recovery codes. Requires a TTY. |
| `reset_platform_admin_totp` | Break-glass re-provisioning — fresh TOTP secret and recovery codes, lockout and replay counter cleared, all admin sessions revoked. Does not change the password. |
| `unlock_platform_admin` | Clears `failed_attempts` / `locked_until` for a locked account and audits the change. The narrow tool — it leaves the password, TOTP secret and recovery codes intact. |

### misc_app

| Command | Description |
|---|---|
| `vacuum_deleted_records` | Renames soft-deleted restaurant records with an `_autodel` suffix and marks them as vacuumed, cascading soft-deletes to child records. |
| `vacuum_configuration` | Not a runnable command — defines configuration (model list and unique-field mappings) used by `vacuum_deleted_records`. |

**Caution:** Several of these commands modify production data, send real email or SMS, or re-provision admin credentials. Run with care outside development environments.

## CI / Testing

CI is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

**What CI runs** (a single-leg Python matrix pinned to **3.12.3**, matching the prod interpreter — on Ubuntu, against a **PostgreSQL 15** service, using `dinify_backend.test_settings`):
1. `pip install -r requirements.txt`
2. `django check`
3. `makemigrations --check --dry-run` — fails on un-generated migrations
4. `python scripts/check_money_fields.py` — fails if a monetary model field is a `FloatField`
5. `python scripts/check_ambient_authority.py` — fails if any customer-plane module reintroduces the retired role-based admin predicates
6. `python scripts/check_tenant_relation_ratchet.py` — fails if the baseline of unclassified writable serializer relations grows
   (each of 4–6 self-tests first and exits 2 — never clean — when its scan or comparison was incomplete)
7. the guard qualification tests: `python -m unittest discover -t . -s scripts -p "tests_*.py"`
8. the tenant-isolation closure gate
9. the **full** test suite: `python -m django test --settings=dinify_backend.test_settings`

**Test database:** PostgreSQL 15 in CI; `test_settings.py` falls back to SQLite in-memory locally when no `DATABASE_*` env vars are set. MongoDB is mocked with `unittest.mock.MagicMock`. `scripts/verify.sh` runs the same checks locally in the same order — run it before opening a PR.

### Test Coverage by App

| App | Has Tests | In CI | Notes |
|---|---|---|---|
| `users_app` | Yes | Yes | Auth flows, OTP, password reset, token security |
| `orders_app` | Yes | Yes | Order initiation, item status, discounts, options |
| `restaurants_app` | Yes | Yes | Largest suite; exercises PostgreSQL-specific `JSONField` lookups |
| `finance_app` | Yes | Yes | Transaction records |
| `misc_app` | Yes | Yes | Config; PostgreSQL `JSONField` lookups |
| `support_app` | Yes | Yes | Support-ticket lifecycle |
| `reviews_app` | Yes | Yes | Review submission, analytics, resolution |
| `platform_admin_app` | Yes | Yes | Admin auth, second factor, delegation, audit log |
| `reports_app` | Yes | Yes | Dashboard, sales, transactions, diners, menu; timezone bucketing |
| `notifications_app` | Yes | Yes | SMS gateway contract |

**To run the full suite locally** (SQLite in-memory, no Postgres needed):
```bash
./scripts/verify.sh
# …or just the tests:
python -m django test --settings=dinify_backend.test_settings --verbosity=2
```

**To run against PostgreSQL** (as CI does), export the `DATABASE_*` env vars first, then run the same test command.

## Breaking Changes

See [`BREAKING_CHANGES.md`](BREAKING_CHANGES.md) for API contract changes that affect the frontend, including:
- Login OTP flow changes (tokens removed from OTP-required response)
- Two-step password reset flow
- Token refresh endpoint
- HTTP status codes now reflect actual errors (previously all returned 200)
- Rate limiting on auth endpoints

## Known Technical Debt

These are issues acknowledged in the codebase as of the current state:

**Money handling:** Resolved — monetary/financial fields use `DecimalField`, and a committed CI guard (`scripts/check_money_fields.py`) fails the build if any `models.py` declares a monetary `FloatField`. (Non-monetary floats remain where appropriate, e.g. floor-plan coordinates.)

**Serializer file size:** Serializer files are large and monolithic. The codebase acknowledges these need splitting into smaller, more manageable files.

**Endpoint handler size:** Endpoint handler files are large. A move toward class-scoped controller functions is acknowledged but not yet done.

**String definitions:** String literals (messages, error text) are scattered across modules rather than centralized.

**Permissions:** `MsisdnLookupEndpoint` uses `AllowAny` — intentional for its use case but warrants review for whether unauthenticated access is appropriate. (The former `AllowAny` `OrderPaymentsEndpoint` / `initiate-order-payment/` write path was retired — endpoint, route, and `OrderPaymentTransaction` controller deleted — to be rebuilt authenticated + ownership-gated at PSP integration.)

**Missing `.env.example` entries:** The MongoDB connection variables (`MONGO_HOST`, `MONGO_DATABASE`) are required by the code but are not listed in `.env.example`.

chore: verify deploy pipeline after host migration
