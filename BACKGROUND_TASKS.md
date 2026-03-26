# Background Tasks & Management Commands — Operations Runbook

**Last verified:** 2026-03-26

This document describes every custom Django management command in the Dinify backend, what it does, what it touches, and where the operational gaps are. It is written for someone who needs to maintain or debug these tasks.

---

## Scheduling

**There is no scheduler configuration in this repository.** No Celery, no Celery Beat, no crontab files, no Procfile, no Docker Compose, no APScheduler, no django-cron. The `requirements.txt` contains no task-queue or scheduling dependencies.

All management commands must be invoked externally — via system crontab, Kubernetes CronJob, CI/CD scheduled pipeline, or manual execution. How and where these commands are currently scheduled in production is not documented in this repo.

One exception: `vacuum_deleted_records` is also called inline (not scheduled) from `misc_app/controllers/secretary.py` after record deletion, with the comment "this is typically doing the cron job inline."

---

## Commands by App

### finance_app

#### `check_dpo_transactions`

| | |
|---|---|
| **Run** | `python manage.py check_dpo_transactions` |
| **Arguments** | None |
| **What it does** | Queries `DinifyTransaction` rows for DPO-aggregated order payments that are still pending (both `processing_status=Pending` and `transaction_status` in Pending/Initiated). Extracts the DPO token from `aggregator_misc_details` and calls `DpoIntegration.verify_token()` for each. |
| **External services** | DPO payment gateway API, PostgreSQL |
| **Idempotency** | Partial. Filters by pending status, so processed transactions are excluded on subsequent runs. No explicit deduplication lock — running twice in quick succession could call the DPO API twice for the same token. Skips transactions where `dpo_token` is None. |
| **Error handling** | **None.** No try/except in the loop. A single DPO API failure crashes the command and skips all remaining transactions. Also has a likely runtime bug: `datetime.datetime.now()` will raise `AttributeError` because `datetime` is imported as the class, not the module. |

#### `verify-dpo-tokens`

| | |
|---|---|
| **Run** | `python manage.py verify-dpo-tokens` |
| **Arguments** | None |
| **What it does** | Nearly identical to `check_dpo_transactions`. Queries DPO order-payment transactions with Pending/Initiated status and calls `DpoIntegration.verify_token()` for each. |
| **External services** | DPO payment gateway API, PostgreSQL |
| **Idempotency** | Weaker than `check_dpo_transactions` — does **not** filter on `processing_status=Pending`, so it may pick up transactions whose processing status has already been updated. |
| **Error handling** | **None.** Uses hard dictionary key lookup (`['transaction_token']`) instead of `.get()`, so a missing key raises `KeyError` and crashes the loop. |
| **Note** | This is functionally a duplicate of `check_dpo_transactions` with slightly different filtering and slightly less safety. The hyphenated filename is non-standard for Django management commands. |

#### `check_transaction_statuses`

| | |
|---|---|
| **Run** | `python manage.py check_transaction_statuses <aggregator>` |
| **Arguments** | `aggregator` (positional, required): `"yo"` or `"dpo"`. Raises `CommandError` if invalid. |
| **What it does** | A more general version of the DPO/Yo checking commands. Queries pending transactions for the specified aggregator (filtering by `processing_status=Pending`, `transaction_status` in Pending/Initiated, non-null `aggregator_reference`) and calls `DpoIntegration.verify_token()` or `YoIntegration.momo_check_transaction()` as appropriate. |
| **External services** | DPO or Yo Payments API (depending on argument), PostgreSQL |
| **Idempotency** | Best of the status-checking commands. Filters on both `processing_status=Pending` and `transaction_status` in Pending/Initiated, and excludes null `aggregator_reference`. |
| **Error handling** | Validates input with `CommandError`. No try/except around per-transaction API calls — a single failure still crashes the loop. |

#### `check_yo_transactions`

| | |
|---|---|
| **Run** | `python manage.py check_yo_transactions` |
| **Arguments** | None |
| **What it does** | Queries Yo-aggregated transactions (order payments and subscriptions) that are pending, and calls `YoIntegration.momo_check_transaction()` for each. |
| **External services** | Yo Payments API, PostgreSQL |
| **Idempotency** | Filters by pending status and non-null `aggregator_reference`. |
| **Error handling** | **None.** Has a likely bug: uses `transaction_type=[...]` (exact match with a list) instead of `transaction_type__in=[...]`, and `processing_status__in=ProcessingStatus_Pending` where the value is a string, not a list. This command may be non-functional. |

#### `process_transactions`

| | |
|---|---|
| **Run** | `python manage.py process_transactions` |
| **Arguments** | None |
| **What it does** | The "second stage" processor. Finds transactions where `transaction_status` is still Pending/Initiated but `processing_status` has already been set to Confirmed or Failed by a payment aggregator. Dispatches to `OrderPaymentTransaction.process()` or `SubscriptionPaymentTransaction.process()` depending on transaction type. This converts aggregator results into business-logic side effects (crediting orders, activating subscriptions, etc.). |
| **External services** | PostgreSQL. No direct external API calls in this file, but the `.process()` methods may touch external services. |
| **Idempotency** | Natural guard: selects rows where `processing_status` is Confirmed/Failed but `transaction_status` is still Pending/Initiated. If `.process()` updates `transaction_status` to a terminal value, those rows won't be picked up again. If `.process()` fails partway without updating status, the transaction will be reprocessed. No explicit deduplication lock. |
| **Error handling** | **None.** No try/except. If `.process()` throws on any transaction, remaining transactions are skipped. Unrecognised `transaction_type` values are silently skipped. |

#### `createaccountswithyo`

| | |
|---|---|
| **Run** | `python manage.py createaccountswithyo` |
| **Arguments** | None |
| **What it does** | Finds `BankAccountRecord` rows where `yo_reference` is NULL and calls `YoIntegration.bank_create_verified_account()` for each to register them with Yo Payments. |
| **External services** | Yo Payments API, PostgreSQL |
| **Idempotency** | Natural guard via `yo_reference__isnull=True` — once a reference is saved, the record won't be selected again. Risk: if the Yo API call succeeds but the reference isn't persisted (crash between API call and DB save), the account will be re-created on the next run. No external idempotency key. |
| **Error handling** | **None.** No try/except. A single Yo API failure crashes the loop. |

#### `seed_dinify_account`

| | |
|---|---|
| **Run** | `python manage.py seed_dinify_account` |
| **Arguments** | None |
| **What it does** | One-time setup command. Creates the singleton `DinifyAccount` record of type `AccountType_DinifyRevenue` if it doesn't already exist. |
| **External services** | PostgreSQL only |
| **Idempotency** | **Fully idempotent.** Checks for existence before creating. Safe to run multiple times. |
| **Error handling** | Catches `DinifyAccount.DoesNotExist`. Does not catch `MultipleObjectsReturned` (would indicate a data integrity issue). |
| **Note** | Help text is copy-pasted from a DPO command and does not describe what this command actually does. |

---

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

### payment_integrations_app

#### `process_aggregator_responses`

| | |
|---|---|
| **Run** | `python manage.py process_aggregator_responses <aggregator>` |
| **Arguments** | `aggregator` (positional, required): `"yo"` or `"dpo"` |
| **What it does** | Reads unprocessed payment callback responses from MongoDB (`yo_responses` or `dpo_responses` collection, filtered by `dinify_processed` not existing). Delegates each to `YoIntegration.process_yo_response()` or `DpoIntegration.process_response()`. |
| **External services** | MongoDB, PostgreSQL (via integration controllers). May call external payment APIs — depends on integration controller internals. |
| **Idempotency** | Partial. Filters by `{'dinify_processed': {'$exists': False}}`. Whether the flag gets set atomically after processing depends entirely on the integration controller code — the command itself does not set it. |
| **Error handling** | **None.** No try/except in the loop. A single processing failure crashes the command. |

---

### reports_app

#### `execute_eod`

| | |
|---|---|
| **Run** | `python manage.py execute_eod` |
| **Arguments** | None |
| **What it does** | Runs the End-of-Day procedure: records EOD start time in `SysActivityConfig`, sets global EOD status to "running", bulk-updates **all** Restaurant records to block new orders (`eod_restaurant_status=1`), calls `initiate_restaurant_eod(eod_date)` (where `eod_date` is yesterday), updates the system business date to today, and calls `generate_daily_reports(eod_date)`. Several planned steps (reconciliation, notifications, archiving) are commented out or TODO. |
| **External services** | PostgreSQL (bulk updates across multiple tables via delegated functions) |
| **Idempotency** | **Not idempotent.** No guard against running EOD twice for the same date. Always computes `eod_date` as yesterday and runs unconditionally. Running it twice would re-block all restaurants and re-trigger EOD processing and report generation. |
| **Error handling** | **None.** No try/except, no `transaction.atomic()`. If any step fails, the system can be left in a broken state: restaurants blocked from accepting orders with EOD incomplete. No rollback mechanism. |

#### `prepare_records`

| | |
|---|---|
| **Run** | `python manage.py prepare_records` |
| **Arguments** | None |
| **What it does** | Transforms string-typed monetary values to rounded floats across three MongoDB archive collections (`archive_transactions`, `archive_accounts`, `archive_orders`). The originally intended user-archiving logic is entirely commented out. |
| **External services** | MongoDB only |
| **Idempotency** | **Good.** Each transform function filters for documents where `transformed_amounts` is false or missing, and sets it to true after processing. Safe to re-run. |
| **Error handling** | Mixed. `transform_account_amounts()` has per-field try/except that logs errors and continues. `transform_transaction_amounts()` and `transform_order_amounts()` have **no** try/except — a single non-numeric value will crash the command. |

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
| `check_dpo_transactions` | finance | DPO API, PG | Partial | None | `datetime` import bug |
| `verify-dpo-tokens` | finance | DPO API, PG | Weak | None | KeyError risk, duplicate of above |
| `check_transaction_statuses` | finance | DPO or Yo API, PG | Good | Input validation only | — |
| `check_yo_transactions` | finance | Yo API, PG | Partial | None | ORM filter bug (likely non-functional) |
| `process_transactions` | finance | PG (+ delegated) | Partial | None | — |
| `createaccountswithyo` | finance | Yo API, PG | Partial | None | — |
| `seed_dinify_account` | finance | PG | Full | Good | Misleading help text |
| `determine-customers` | orders | PG | Good | Partial | Atomic rollback risk |
| `send_messages` | notifications | MongoDB, SMTP, Yo SMS | Partial | None | Re-send risk on crash |
| `process_aggregator_responses` | payments | MongoDB, PG, APIs | Partial | None | — |
| `execute_eod` | reports | PG (bulk) | **None** | **None** | Can leave system in broken state |
| `prepare_records` | reports | MongoDB | Good | Mixed | — |
| `vacuum_deleted_records` | misc | PG | Good | Minimal | — |

---

## Operational Gaps

### No scheduling infrastructure

There is no Celery, Celery Beat, crontab, Procfile, or any other scheduler in this repository. All management commands are presumably scheduled externally (system cron, Kubernetes CronJobs, etc.), but that configuration is not documented or version-controlled here. If the external scheduler breaks or is misconfigured, there is no way to tell from this repo alone what should be running and when.

### No retry behaviour

No command implements retry logic. If an external API call fails (DPO, Yo, SMTP), the command crashes immediately and all remaining items in the batch are skipped. There are no dead-letter queues, no exponential backoff, no retry counters. Recovery depends entirely on re-running the command on the next scheduled invocation and hoping the failed item's status hasn't changed.

### No per-item error isolation

With the exception of `vacuum_deleted_records` (partial) and `prepare_records` (partial), every command that loops over items and calls external services will crash on the first failure. Items after the failure point are never processed until the next run. For `execute_eod`, a mid-process crash leaves the system in a partially-completed EOD state with no automated recovery.

### No monitoring or alerting

No command emits metrics, health checks, or alert signals. There are no Prometheus counters, no Datadog tags, no Sentry breadcrumbs. If a command fails silently (e.g. the ORM filter bug in `check_yo_transactions` causes it to process zero records), there is no mechanism to detect this.

### No failure notifications

When a command fails, no email, Slack message, or other notification is sent. Operators must either check logs manually or rely on the external scheduler to report non-zero exit codes (if it is configured to do so).

### Logging

All commands use `print()` instead of Python's `logging` module or Django's `self.stdout.write()`. This means:
- Output does not include timestamps, log levels, or structured fields
- Output cannot be routed to log aggregation systems without additional tooling
- There is no way to distinguish informational messages from errors in the output

### Idempotency gaps

- `execute_eod` has **no** idempotency protection — running it twice for the same date will re-process everything
- `send_messages` can re-send emails if the command crashes after sending but before marking as sent in MongoDB
- `createaccountswithyo` can create duplicate Yo accounts if the command crashes after the API call but before persisting the reference
- `check_dpo_transactions` and `verify-dpo-tokens` can call the DPO API multiple times for the same token across concurrent runs

### Duplicate commands

`check_dpo_transactions`, `verify-dpo-tokens`, and `check_transaction_statuses dpo` all perform essentially the same function (check DPO transaction status) with slightly different filtering logic. This creates confusion about which one to use and maintenance burden when behaviour needs to change.

### Likely bugs

- `check_dpo_transactions`: `datetime.datetime.now()` will raise `AttributeError` (imports `datetime` class, not module)
- `check_yo_transactions`: ORM filter uses `=` with a list instead of `__in`, and passes a string to `__in` — likely returns zero results or errors
- `seed_dinify_account`: Help text is copy-pasted from a DPO command and does not describe the actual functionality
- `verify-dpo-tokens`: Hard dictionary key access (`['transaction_token']`) instead of `.get()` will crash on missing keys
