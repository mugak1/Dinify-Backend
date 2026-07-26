# Background Tasks & Management Commands — Operations Runbook

**Last verified:** 2026-07-20

This document describes every custom Django management command in the Dinify backend, what it does, what it touches, and where the operational gaps are. It is written for someone who needs to maintain or debug these tasks.

---

## Scheduling

**There is no scheduler configuration in this repository.** No Celery, no Celery Beat, no crontab files, no Procfile, no Docker Compose, no APScheduler, no django-cron. The `requirements.txt` contains no task-queue or scheduling dependencies.

All management commands must be invoked externally — via system crontab, Kubernetes CronJob, CI/CD scheduled pipeline, or manual execution. How and where these commands are currently scheduled in production is not documented in this repo.

One exception: `vacuum_deleted_records` is also called inline (not scheduled) from `misc_app/controllers/secretary.py` after record deletion, with the comment "this is typically doing the cron job inline."

---

## Commands by App

### orders_app

#### `determine-customers`

| | |
|---|---|
| **Run** | `python manage.py determine-customers` |
| **Arguments** | None |
| **What it does** | Matches orders that have no assigned customer to existing User records. For each order: checks `customer_phone` and `customer_email`; if both are null, looks up the associated `DinifyTransaction` for an MSISDN. Tries to find an existing User by phone or email. If no match is found, **creates a new User** with a random 6-digit password. Associates the customer with the order and sets `customer_match_attempted=True`. Entire batch runs inside `transaction.atomic()`. |
| **External services** | PostgreSQL only |
| **Idempotency** | **Good.** Filters by `customer=None, customer_match_attempted=False`. The flag is set regardless of outcome. |
| **Error handling** | Partial. `User.objects.get()` lookups are wrapped in try/except. However, if `User.objects.create()` fails (e.g. unique constraint violation), the exception is **not** caught, and because the entire loop is in one `transaction.atomic()` block, **all work in the batch is rolled back**. |

---

### notifications_app

#### `send_messages`

| | |
|---|---|
| **Run** | `python manage.py send_messages` |
| **Arguments** | None |
| **What it does** | Reads unsent notifications from MongoDB (`notifications` collection where `sent` field does not exist). For each: sends an HTML email via Django's SMTP backend. If the notification is a "Dinify Credentials!" message and has SMS content, also sends an SMS via the Yo Uganda SMS gateway (`smgw1.yo.co.ug` HTTP GET). Marks the MongoDB document as `sent: True` after sending. Checks that the owner's restaurant is active before sending credential notifications. |
| **External services** | MongoDB, SMTP email, Yo Uganda SMS gateway (HTTP GET), PostgreSQL (Restaurant status check) |
| **Idempotency** | Partial. Filters by `{"sent": {"$exists": False}}`. However, the `sent` flag is set **after** sending. If the command crashes after sending an email but before updating MongoDB, the message will be re-sent on the next run. No deduplication at the email/SMS level. |
| **Error handling** | **None.** No try/except in the loop. Email uses `fail_silently=False`, so a single SMTP failure crashes the command and all subsequent notifications are skipped. The SMS helper has its own try/except for HTTP errors, but the email path does not. |

---

### misc_app

#### `vacuum_deleted_records`

| | |
|---|---|
| **Run** | `python manage.py vacuum_deleted_records` |
| **Arguments** | None |
| **What it does** | Processes soft-deleted records (`deleted=True, vacuumed=False`) across six restaurant-related models (Restaurant, MenuSection, SectionGroup, MenuItem, DiningArea, Table). For each: performs a "soft cascade" (marks child records as deleted), renames the record by appending `_autodel{N}`, sets `vacuumed=True`. |
| **External services** | PostgreSQL only |
| **Idempotency** | **Good.** Filters by `deleted=True, vacuumed=False`. Once processed, `vacuumed` is set to True. |
| **Error handling** | Minimal. Has a try/except around name-length truncation that logs and continues. No try/except around `rec.save()` — a database error on any record crashes the command. No `transaction.atomic()`, so partial work persists. |

#### `vacuum_configuration`

This is **not a runnable command**. It is a configuration module that defines `VACUUM_MODELS` — a list of model-to-field mappings used by `vacuum_deleted_records`. It is imported, not executed.

---

### platform_admin_app

These two are **operator commands, not scheduled tasks** — they are run by hand, over SSH, on the box. Both are interactive by design and both refuse to run without a valid `ADMIN_SECRET_ENCRYPTION_KEY`, because provisioning that half-completes would leave an administrator who can never sign in.

#### `create_platform_admin`

| | |
|---|---|
| **Run** | `python manage.py create_platform_admin --username <u> --email <e> --full-name "<name>"` |
| **Arguments** | `--username`, `--email`, `--full-name`. The password is **prompted**, never passed in argv (argv lands in shell history and the process table). |
| **What it does** | Creates a `platform_staff` User plus its `PlatformStaffAuth` row, enrols a TOTP secret (encrypted at rest), and prints the `otpauth://` URI, an ASCII QR, and ten one-time recovery codes. Refuses a duplicate username/email, a phone-number username, or a missing encryption key. |
| **Requires** | A TTY (it prompts) and `ADMIN_SECRET_ENCRYPTION_KEY`. |
| **Idempotency** | N/A — refuses to run twice for the same account. |

#### `reset_platform_admin_totp`

| | |
|---|---|
| **Run** | `python manage.py reset_platform_admin_totp --username <u>` (add `--noinput` to skip the typed confirmation in a scripted runbook) |
| **Arguments** | `--username` (required), `--noinput` |
| **What it does** | **Break-glass re-provisioning.** Generates a fresh TOTP secret and ten fresh recovery codes, clears the replay counter and any lockout, and revokes every active admin session. Prints the new QR + codes once. Writes `admin.auth.totp_reset`, `admin.auth.recovery_codes_generated` and (if any were live) `admin.session.revoked` audit rows in one transaction. |
| **Does NOT** | Change the password. Decrypt the existing secret — it re-provisions from scratch, which is why it survives key loss. |
| **Requires** | `ADMIN_SECRET_ENCRYPTION_KEY` — it **encrypts** the new secret, so the key must be valid *before* it runs. |
| **Idempotency** | Safe to re-run; each run invalidates the previous authenticator and code set. |

#### `unlock_platform_admin`

| | |
|---|---|
| **Run** | `python manage.py unlock_platform_admin --username <u>` |
| **Arguments** | `--username` (required) |
| **What it does** | Clears `failed_attempts` and `locked_until` for a platform-staff account, under a row lock, and writes one `admin.auth.lockout_cleared` audit row recording the before/after counts. |
| **Does NOT** | Touch the password, the TOTP secret or the recovery codes. This is the narrow tool — reaching for `reset_platform_admin_totp` to undo a lockout destroys the authenticator and all ten recovery codes for no reason. |
| **Requires** | Nothing beyond database access. It never decrypts, so it works during a Fernet-key incident too. |
| **Idempotency** | **Good.** Safe on an already-unlocked account; says so and changes nothing. |

##### Nuisance lockouts, and the two ways out

Lockout is durable and per-account (`PlatformStaffAuth.failed_attempts` / `locked_until`) because the DRF throttles are per-process and reset on restart. Policy: **10 cumulative failures, then the window doubles per further failure** — 10th → 1 min, 11th → 2, 12th → 4, 13th → 8, 14th → 16, 15th → 32, 16th and beyond → 60 (cap). Wrong passwords and wrong second factors advance the same counter.

The counter is **cumulative**: waiting out a window does not forgive it, so the next failure re-locks at the next step up. That is deliberate — it is what makes the backoff escalate instead of resetting to one minute forever.

With one administrator and a discoverable username, anyone who learns it can push the account to the 60-minute cap and keep it there. Two escapes exist, and **neither is available to a lockout attacker**, because both need a secret they do not hold:

1. **Over HTTP — password + a one-shot recovery code.** Sign in normally: a locked account with the *correct* password receives a challenge whose response carries `recovery_code_required: true`. Send `{"method": "recovery", "code": "<one-of-your-ten>"}` to `auth/verify/`. This clears the lock and signs you in (`lockout_cleared: true`). A TOTP code is refused against such a challenge — TOTP is what an attacker can make you fail; a recovery code is not.
2. **On the box — `python manage.py unlock_platform_admin --username <u>`.** Use this when you would rather not spend a recovery code.

Both are audited as `admin.auth.lockout_cleared`.

##### Break-glass: recovering when `ADMIN_SECRET_ENCRYPTION_KEY` is lost

`ADMIN_SECRET_ENCRYPTION_KEY` encrypts the TOTP secret at rest. **While that key is missing or corrupt, TOTP cannot be verified at all — the stored secret is unreadable, so even a correct 6-digit code is refused. Recovery codes are the only way in.** They work because the recovery path never touches the key (`platform_admin_app/second_factor.py`); do not "simplify" that by letting a recovery attempt fall through TOTP.

Run these in order. Step 3 needs the new key, which is why it comes after step 2:

1. **Sign in with a recovery code.** Works with the key missing.
   ```bash
   curl -sk -c jar https://admin.dinifyapp.com/api/admin/v1/auth/login/ \
     -H 'Content-Type: application/json' \
     -d '{"username":"<u>","password":"<p>"}'
   curl -sk -b jar -c jar https://admin.dinifyapp.com/api/admin/v1/auth/verify/ \
     -H 'Content-Type: application/json' \
     -d '{"method":"recovery","code":"<one-of-your-ten-codes>"}'
   ```
   The response reports `recovery_codes_remaining`. Each code works exactly once.
2. **Install a new key** in the project `.env` (see `.env.example`). Generate one with
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Keep the file's ownership and mode as the deploy gates require — `ubuntu:www-data`, `640`.
   The old ciphertext is **not** readable under the new key; that is expected, and why the next step re-provisions rather than re-encrypts.
3. **Re-provision:** `python manage.py reset_platform_admin_totp --username <u>`.
   This revokes all sessions, so the session from step 1 ends here.
4. **Re-enrol** the authenticator from the printed QR, and **store the ten new recovery codes offline** (print them, or write them down and put them somewhere physically safe). They are not recoverable from the database — only their hashes are stored.

This sequence is covered end to end by `platform_admin_app/tests_second_factor.py::BreakGlassSequenceTests`. If the sequence changes, that test changes with it.

**Future item, not implemented:** key rotation via `MultiFernet` (decrypt under an old key, re-encrypt under a new one) would let a *planned* key change avoid re-enrolment entirely. Today there is one key and no rotation path, so a lost key always means re-provisioning.

---

## Command Summary Table

| Command | App | External Services | Idempotent | Error Handling | Likely Bugs |
|---|---|---|---|---|---|
| `determine-customers` | orders | PG | Good | Partial | Atomic rollback risk |
| `send_messages` | notifications | MongoDB, SMTP, Yo SMS | Partial | None | Re-send risk on crash |
| `vacuum_deleted_records` | misc | PG | Good | Minimal | — |
| `create_platform_admin` | platform_admin | PG | N/A (refuses duplicates) | Fail-closed | — |
| `reset_platform_admin_totp` | platform_admin | PG | Good (re-runnable) | Fail-closed | — |
| `unlock_platform_admin` | platform_admin | PG | Good | Fail-closed | — |

---

## Operational Gaps

### No scheduling infrastructure

There is no Celery, Celery Beat, crontab, Procfile, or any other scheduler in this repository. All management commands are presumably scheduled externally (system cron, Kubernetes CronJobs, etc.), but that configuration is not documented or version-controlled here. If the external scheduler breaks or is misconfigured, there is no way to tell from this repo alone what should be running and when.

### No retry behaviour

No command implements retry logic. If an external call fails (SMTP or the SMS gateway), the command crashes immediately and all remaining items in the batch are skipped. There are no dead-letter queues, no exponential backoff, no retry counters. Recovery depends entirely on re-running the command on the next scheduled invocation.

### No per-item error isolation

With the exception of `vacuum_deleted_records` (partial), every command that loops over items and calls external services will crash on the first failure. Items after the failure point are never processed until the next run.

### No monitoring or alerting

No command emits metrics, health checks, or alert signals. There are no Prometheus counters, no Datadog tags, no Sentry breadcrumbs. If a command fails silently (e.g. processes zero records because a filter matches nothing), there is no mechanism to detect this.

### No failure notifications

When a command fails, no email, Slack message, or other notification is sent. Operators must either check logs manually or rely on the external scheduler to report non-zero exit codes (if it is configured to do so).

### Logging

All commands use `print()` instead of Python's `logging` module or Django's `self.stdout.write()`. This means:
- Output does not include timestamps, log levels, or structured fields
- Output cannot be routed to log aggregation systems without additional tooling
- There is no way to distinguish informational messages from errors in the output

### Idempotency gaps

- `send_messages` can re-send emails if the command crashes after sending but before marking as sent in MongoDB
