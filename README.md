# Dinify Backend

Django REST API backend for the Dinify restaurant management and ordering platform.

## Tech Stack

| Component | Version / Package |
|---|---|
| Python | 3.10.12 (CI, pinned to match the prod EC2 runtime) — 3.10+ locally |
| Django | 4.2.30 |
| Django REST Framework | 3.17.1 |
| Auth | `djangorestframework-simplejwt` 5.5.1 (JWT Bearer tokens) |
| Database (primary) | PostgreSQL via `psycopg` 3.1.18 |
| Database (document store) | MongoDB via `pymongo` 4.6.3 |
| HTTP client | `requests` 2.34.2 |
| Image handling | `Pillow` 12.2.0 |
| Data processing | `pandas` 2.2.3, `numpy` 2.0.2 |
| CORS | `django-cors-headers` 4.3.1 |
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

**Note:** Some features (notifications, payment callback storage, action logs) require a running MongoDB instance. The connection is configured via `MONGO_HOST` and `MONGO_DATABASE` environment variables — see `dinify_backend/mongo_db.py`. The `.env.example` file does not currently include these variables; you will need to add them manually if you use MongoDB-dependent features.

## Architecture Overview

The project uses a single Django settings file (`dinify_backend/settings.py`) with one PostgreSQL database. MongoDB is used as a secondary document store for notifications, payment provider callbacks, and action logs.

### Installed Django Apps

| App | Purpose |
|---|---|
| `users_app` | Custom user model (`AUTH_USER_MODEL`), authentication (login, OTP verification, password reset), user profile management, and role-based access. Defines the `BaseModel` abstract class used by most other models (with audit fields and soft-delete). |
| `restaurants_app` | Restaurant CRUD, employee management, menu structure (sections, groups, items with options and extras), dining areas, and tables. Also handles restaurant subscription configuration. |
| `orders_app` | Order creation, item-level status tracking, pricing/discount calculations, customer assignment, and order ratings/reviews. |
| `finance_app` | Financial transaction processing, wallet/account management (`DinifyAccount` with multi-mode balances), bank account records, subscription and order payment logic, and disbursements. |
| `payment_integrations_app` | Integrations with external payment providers: Flutterwave, DPO, Yo Uganda (mobile money), and Pesapal. Handles payment initiation, callback processing, and status verification. Has no Django models — uses MongoDB for callback storage. |
| `notifications_app` | Email and SMS dispatch. Reads unsent notifications from MongoDB and sends them. Has no Django models. |
| `reports_app` | End-of-day processing and report generation. Has no Django models currently — report logic operates on other apps' data. |
| `support_app` | Restaurant-facing support ticketing (`SupportIssue`, collision-safe `SUP-000123` references). Secretary-pattern endpoints at `api/v1/support/`. |
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
| CORS | `CORS_ORIGIN_ALLOW_ALL`, `CORS_ALLOWED_ORIGINS` | Cross-origin request policy |
| JWT | `JWT_ACCESS_LIFETIME_MINUTES`, `JWT_REFRESH_LIFETIME_DAYS` | Token expiry configuration |
| Database | `DATABASE_ENGINE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT` | PostgreSQL connection |
| Email | `EMAIL_HOST`, `EMAIL_ACCOUNT`, `EMAIL_PASSWORD`, `EMAIL_PORT` | SMTP email dispatch |

**Not in `.env.example` but referenced in code (via `test_settings.py` and integration modules):**

| Group | Variables | Purpose |
|---|---|---|
| MongoDB | `MONGO_HOST`, `MONGO_DATABASE` | MongoDB connection for notifications, callbacks, and logs |
| Yo Uganda | `YO_SMS_ACCOUNT_NO`, `YO_SMS_PASSWORD` | Yo SMS gateway (OTP + notifications) |
| Rate limiting | `THROTTLE_AUTH_LOGIN`, `THROTTLE_AUTH_OTP`, `THROTTLE_AUTH_RESET` | Auth endpoint throttle rates (defaults: 10/min, 5/min, 5/min) |

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

### finance_app

| Command | Description |
|---|---|
| `check_dpo_transactions` | Queries pending/initiated DPO order-payment transactions and verifies their token status with the DPO gateway. |
| `verify-dpo-tokens` | Similar to `check_dpo_transactions` — fetches pending DPO transactions and verifies each token with DPO (slightly different filtering). |
| `check_transaction_statuses` | Accepts an aggregator argument (`yo` or `dpo`) and checks payment status of all pending transactions with that aggregator's API. |
| `check_yo_transactions` | Queries pending/initiated Yo mobile-money transactions and checks their status via the Yo integration API. |
| `process_transactions` | Finds transactions with confirmed or failed processing status that are still pending/initiated, and runs the appropriate payment or subscription processing logic. |
| `createaccountswithyo` | Registers bank account records that lack a Yo reference as verified accounts with the Yo payments integration. |
| `seed_dinify_account` | Creates the singleton Dinify revenue account if it does not already exist. |

### orders_app

| Command | Description |
|---|---|
| `determine-customers` | Matches orders with no assigned customer to existing users by phone/email, or creates new user records, then links the customer to the order. |

### notifications_app

| Command | Description |
|---|---|
| `send_messages` | Reads unsent notifications from MongoDB and sends them as emails (and optionally SMS for credential notifications), then marks each as sent. |

### payment_integrations_app

| Command | Description |
|---|---|
| `process_aggregator_responses` | Accepts an aggregator argument (`yo` or `dpo`), reads unprocessed payment callback responses from MongoDB, and processes each through the corresponding integration handler. |

### misc_app

| Command | Description |
|---|---|
| `vacuum_deleted_records` | Renames soft-deleted restaurant records with an `_autodel` suffix and marks them as vacuumed, cascading soft-deletes to child records. |
| `vacuum_configuration` | Not a runnable command — defines configuration (model list and unique-field mappings) used by `vacuum_deleted_records`. |

**Caution:** Many of these commands interact with live payment APIs or modify production data. Run with care outside development environments.

## CI / Testing

CI is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

**What CI runs** (Python 3.10.12 on Ubuntu, against a **PostgreSQL 15** service, using `dinify_backend.test_settings`):
1. `pip install -r requirements.txt`
2. `django check`
3. `makemigrations --check --dry-run` — fails on un-generated migrations
4. `python scripts/check_money_fields.py` — fails if a monetary model field is a `FloatField`
5. the **full** test suite: `python -m django test --settings=dinify_backend.test_settings`

**Test database:** PostgreSQL 15 in CI; `test_settings.py` falls back to SQLite in-memory locally when no `DATABASE_*` env vars are set. MongoDB is mocked with `unittest.mock.MagicMock`. `scripts/verify.sh` runs the same checks locally in the same order — run it before opening a PR.

### Test Coverage by App

| App | Has Tests | In CI | Notes |
|---|---|---|---|
| `users_app` | Yes | Yes | Auth flows, OTP, password reset, token security |
| `orders_app` | Yes | Yes | Order initiation, item status, discounts, options |
| `payment_integrations_app` | Yes | Yes | HTTP timeout safety, network/XML handling, credential protection |
| `restaurants_app` | Yes | Yes | Largest suite; exercises PostgreSQL-specific `JSONField` lookups |
| `finance_app` | Yes | Yes | Transactions, balances, payments |
| `misc_app` | Yes | Yes | Config; PostgreSQL `JSONField` lookups |
| `support_app` | Yes | Yes | Support-ticket lifecycle |
| `notifications_app` | No | No | No tests written |
| `reports_app` | No | No | No tests written |

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

**Hardcoded payment URLs:** Yo Uganda sandbox (`sandbox.yo.co.ug`) and Pesapal sandbox (`cybqa.pesapal.com`) URLs are hardcoded. DPO redirect URL (`https://dinify-web`) is incomplete/placeholder. These need environment-based configuration before production use.

**Permissions:** `MsisdnLookupEndpoint` uses `AllowAny` — intentional for its use case but warrants review for whether unauthenticated access is appropriate. (The former `AllowAny` `OrderPaymentsEndpoint` / `initiate-order-payment/` write path was retired — endpoint, route, and `OrderPaymentTransaction` controller deleted — to be rebuilt authenticated + ownership-gated at PSP integration.)

**Test gaps:** Two apps have no tests at all (`notifications_app`, `reports_app`). The full suite now runs in CI against PostgreSQL 15, so every app that *does* have tests is exercised there.

**Missing `.env.example` entries:** MongoDB connection variables and all payment integration credentials are required by the code but not listed in `.env.example`.
