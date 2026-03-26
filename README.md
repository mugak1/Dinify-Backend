# Dinify Backend

Django REST API backend for the Dinify restaurant management and ordering platform.

## Tech Stack

| Component | Version / Package |
|---|---|
| Python | 3.11 (CI) — 3.9.6+ may work locally |
| Django | 4.2.9 |
| Django REST Framework | 3.14.0 |
| Auth | `djangorestframework-simplejwt` 5.3.1 (JWT Bearer tokens) |
| Database (primary) | PostgreSQL via `psycopg` 3.1.18 |
| Database (document store) | MongoDB via `pymongo` 4.6.1 |
| HTTP client | `requests` 2.32.3 |
| Image handling | `Pillow` 10.2.0 |
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
| `crm_app` | Customer support ticket management (`ServiceTicket` model with status, priority, and assignment tracking). |
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
| CORS | `CORS_ORIGIN_ALLOW_ALL`, `CORS_ALLOWED_ORIGINS` | Cross-origin request policy |
| JWT | `JWT_ACCESS_LIFETIME_MINUTES`, `JWT_REFRESH_LIFETIME_DAYS` | Token expiry configuration |
| Database | `DATABASE_ENGINE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT` | PostgreSQL connection |
| Email | `EMAIL_HOST`, `EMAIL_ACCOUNT`, `EMAIL_PASSWORD`, `EMAIL_PORT` | SMTP email dispatch |

**Not in `.env.example` but referenced in code (via `test_settings.py` and integration modules):**

| Group | Variables | Purpose |
|---|---|---|
| MongoDB | `MONGO_HOST`, `MONGO_DATABASE` | MongoDB connection for notifications, callbacks, and logs |
| Flutterwave | `FLUTTERWAVE_SECRET`, `DEFAULT_PAYMENT_EMAIL` | Flutterwave payment integration |
| DPO | `DPO_COMPANY_TOKEN`, `DPO_SERVICE_TYPE` | DPO payment gateway |
| Yo Uganda | `YO_API_USERNAME`, `YO_API_PASSWORD`, `YO_SMS_ACCOUNT_NO`, `YO_SMS_PASSWORD` | Yo mobile money and SMS |
| Pesapal | `PESAPAL_CONSUMER_KEY`, `PESAPAL_CONSUMER_SECRET` | Pesapal payment integration |
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

### reports_app

| Command | Description |
|---|---|
| `execute_eod` | Runs end-of-day procedure: blocks new orders, advances the business date, initiates per-restaurant EOD processing, and generates daily reports. |
| `prepare_records` | Intended for record archival (user archiving code is commented out); currently calls `transform_amounts()` to transform report amount data. |

### misc_app

| Command | Description |
|---|---|
| `vacuum_deleted_records` | Renames soft-deleted restaurant records with an `_autodel` suffix and marks them as vacuumed, cascading soft-deletes to child records. |
| `vacuum_configuration` | Not a runnable command — defines configuration (model list and unique-field mappings) used by `vacuum_deleted_records`. |

**Caution:** Many of these commands interact with live payment APIs or modify production data. Run with care outside development environments.

## CI / Testing

CI is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

**What CI runs:**
- Python 3.11 on Ubuntu
- Installs dependencies from `requirements.txt`
- Runs: `python -m django test users_app orders_app payment_integrations_app --settings=dinify_backend.test_settings`

**Test database:** SQLite in-memory (via `test_settings.py`). MongoDB is mocked with `unittest.mock.MagicMock`.

### Test Coverage by App

| App | Has Tests | In CI | Notes |
|---|---|---|---|
| `users_app` | Yes | Yes | Auth flows, OTP, password reset, token security |
| `orders_app` | Yes | Yes | Order initiation, item status, discounts, options |
| `payment_integrations_app` | Yes | Yes | HTTP timeout safety, network error handling, XML parsing, credential protection |
| `restaurants_app` | Yes | **No** | Uses `JSONField.__contains` lookups requiring PostgreSQL — cannot run under SQLite CI |
| `finance_app` | Yes | **No** | Has a bug (`resend_otp` leaves `user=None`) and calls live Yo API — not CI-ready without fixes and mocking |
| `misc_app` | Yes | **No** | Uses `JSONField.__contains` lookups requiring PostgreSQL — cannot run under SQLite CI |
| `crm_app` | No | No | No tests written |
| `notifications_app` | No | No | No tests written |
| `reports_app` | No | No | No tests written |

**To run tests locally (against SQLite):**
```bash
python -m django test users_app orders_app payment_integrations_app --settings=dinify_backend.test_settings --verbosity=2
```

**To run all tests (requires local PostgreSQL):**
```bash
python manage.py test --verbosity=2
```

## Breaking Changes

See [`BREAKING_CHANGES.md`](BREAKING_CHANGES.md) for API contract changes that affect the frontend, including:
- Login OTP flow changes (tokens removed from OTP-required response)
- Two-step password reset flow
- Token refresh endpoint
- HTTP status codes now reflect actual errors (previously all returned 200)
- Rate limiting on auth endpoints

## Known Technical Debt

These are issues acknowledged in the codebase as of the current state:

**Money handling:** All 19 monetary fields across `orders_app` and `restaurants_app` use `FloatField` instead of `DecimalField`. Float arithmetic is used throughout order calculations, risking rounding errors. Fixing requires a database migration and serializer audit.

**Serializer file size:** Serializer files are large and monolithic. The codebase acknowledges these need splitting into smaller, more manageable files.

**Endpoint handler size:** Endpoint handler files are large. A move toward class-scoped controller functions is acknowledged but not yet done.

**String definitions:** String literals (messages, error text) are scattered across modules rather than centralized.

**Hardcoded payment URLs:** Yo Uganda sandbox (`sandbox.yo.co.ug`) and Pesapal sandbox (`cybqa.pesapal.com`) URLs are hardcoded. DPO redirect URL (`https://dinify-web`) is incomplete/placeholder. These need environment-based configuration before production use.

**Permissions:** `OrderPaymentsEndpoint` and `MsisdnLookupEndpoint` use `AllowAny` — intentional for their use cases but warrant review for whether unauthenticated access is appropriate.

**CI gaps:** Three apps with tests (`restaurants_app`, `finance_app`, `misc_app`) cannot run in CI due to SQLite limitations or live API calls. Three apps (`crm_app`, `notifications_app`, `reports_app`) have no tests at all.

**Missing `.env.example` entries:** MongoDB connection variables and all payment integration credentials are required by the code but not listed in `.env.example`.
