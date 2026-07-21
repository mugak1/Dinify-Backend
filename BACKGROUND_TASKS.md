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

## Command Summary Table

| Command | App | External Services | Idempotent | Error Handling | Likely Bugs |
|---|---|---|---|---|---|
| `determine-customers` | orders | PG | Good | Partial | Atomic rollback risk |
| `send_messages` | notifications | MongoDB, SMTP, Yo SMS | Partial | None | Re-send risk on crash |
| `vacuum_deleted_records` | misc | PG | Good | Minimal | — |

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
