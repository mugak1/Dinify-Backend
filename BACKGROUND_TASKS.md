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
| **What it does** | Matches orders that have no assigned customer to existing User records. For each order: uses the order's own `customer_phone` and `customer_email`; only if **both are null** does it fall back to the `msisdn` of the order's `DinifyTransaction` payment. Tries to find an existing User by phone, then by email. If no match is found and the order has a usable phone, **creates a new User** keyed on that phone, with a random 6-digit password. An email alone never creates one. Associates the customer with the order and sets `customer_match_attempted=True`. Entire batch runs inside `transaction.atomic()`. |
| **Matching rules** | The phone is canonicalised with `normalise_msisdn` against the restaurant's `country`. An unnormalisable phone is skipped, not fatal, and the order can still match on its email; the log line never carries the number. The email is resolved through `users_app.controllers.email_lookup.get_user_by_email`, the rule login and password reset use: the address as typed first, then lower-cased if exactly one account holds it. So a differently-cased order email matches the account registration stored lower-cased. An exactly-typed address still matches its own account beside a lower-cased twin, and an address several accounts share matches none of them. A blank or whitespace-only value counts as no contact at all, because `create_user` stores a missing email as `''` and a blank one would otherwise match an arbitrary account. A blank value does **not** trigger the payment fallback, which still asks only whether both fields are null. **An account is created only from a usable phone.** An unmatched email with no usable phone (none, blank or unnormalisable) leaves the order unmatched and marked attempted, because a restaurant user's phone is required at every write site and is its identity. An email that arrives beside an unmatched phone is stored on the new account as it arrived. |
| **External services** | PostgreSQL only |
| **Idempotency** | **Good.** Filters by `customer=None, customer_match_attempted=False`. The flag is set regardless of outcome. Until the change that added `orders_app/tests_determine_customers.py`, the order's own phone and email were never read, a defect dating from the command's first version. So an order that a run processed before then was marked attempted without them and is **not** retried. Re-matching such orders means clearing the flag on them, a deliberate operation this command does not perform. The diner client has never sent either field, so few if any such rows are expected. That is an inference from source history, not a count. |
| **Error handling** | Partial. `User.objects.get()` lookups are wrapped in try/except. However, if `User.objects.create()` fails (e.g. unique constraint violation), the exception is **not** caught, and because the entire loop is in one `transaction.atomic()` block, **all work in the batch is rolled back**. Every account it creates is keyed on the canonical phone as both `phone_number` and `username`, and a phone some account already holds is matched rather than created. So the remaining collision is an account whose `username` is that number while its `phone_number` differs. |

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

These are **operator commands, not scheduled tasks** — they are run by hand, over SSH, on the box. Nothing here is on a timer, and nothing here should be put on one.

The two provisioning commands (`create_platform_admin`, `reset_platform_admin_totp`) are interactive by design and both refuse to run without a valid `ADMIN_SECRET_ENCRYPTION_KEY`, because provisioning that half-completes would leave an administrator who can never sign in. The other three (`unlock_platform_admin`, `mark_restaurant_test`, `adopt_restaurant_onboarding`) need nothing beyond database access.

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

#### `mark_restaurant_test`

| | |
|---|---|
| **Run** | `python manage.py mark_restaurant_test --restaurant <UUID> --test true\|false --actor <platform-staff-username> --reason "<why>"` |
| **Arguments** | All four are **required**. `--restaurant` is the restaurant's UUID. `--test` is exactly `true` or `false` (case-folded; nothing else — no `1`, `yes`, `on`). `--actor` is the username of the platform-staff human making the decision. `--reason` is a free-text justification of at least 10 characters after trimming (the same bar as a lifecycle transition and a delegation grant). |
| **What it does** | Sets or clears `Restaurant.is_test` for **exactly one** restaurant and writes one `admin.restaurant.test_classification_changed` audit row carrying the actor, the reason and the before/after booleans. The write and the audit row share one `transaction.atomic()`, so a failed audit rolls the classification back. That transaction takes the exclusive admission advisory lock first and the `Restaurant` row lock second — the same order as a lifecycle transition — so an order cannot be admitted against one classification and then written under the other. |
| **Why it exists** | `Restaurant.is_test` is platform-owned metadata: it FLAGS a tenant's future orders as test orders (`Order.is_test` derives from it at admission). A test restaurant can still do everything a live one can — its flagged orders count in its own reports and dashboards, can be reviewed and are matched to customers; only a test order at a REAL restaurant (a practice order) is left out, see `orders_app/controllers/test_orders.py`. Migration 0057 added it with no backfill and deliberately no name heuristic, and there is no Admin UI for it yet. This is the audited alternative to somebody running a `.update()` in a shell with no actor, no reason and no record. |
| **Targeting** | **UUID only.** There is no `--restaurant-name`, no fuzzy match, no `.first()`, and no bulk mode. A malformed UUID, an unknown UUID and a soft-deleted restaurant are all refused, the last two with the same message so the command never confirms a soft-deleted tenant exists. |
| **Actor** | Must exist, be `account_type = platform_staff`, and be active — otherwise the command fails before touching anything. This is **attribution, not authentication**: anyone who can run `manage.py` on the box already has more authority than this command grants. It asks for no password, no TOTP code and no recovery code, and prints no secrets. |
| **Changes ONLY** | `Restaurant.is_test` (plus the row's `auto_now` `time_last_updated` stamp). Not `status` — that is `restaurants_app.controllers.lifecycle`'s sole privilege — and not the owner, the soft-delete flag, tables, menus, QR state, employees or users. |
| **Does NOT rewrite history** | Existing `Order.is_test` rows are left exactly as they are. Classification governs how **future** orders are derived at admission time; retro-labelling past orders would silently restate historical revenue and is a separate decision needing its own migration. |
| **Not reachable by tenants** | There is no customer-plane path to this flag at all: it is absent from both `EDIT_INFORMATION['restaurants']` and `SerializerPutRestaurant`, so no restaurant user can set it through the API, and this command is shell-only. |
| **External services** | PostgreSQL only. No SMS, no email, no MongoDB, no encryption key. |
| **Idempotency** | **Good.** Re-running the same classification changes nothing, writes no second audit row, prints `Restaurant already has is_test=<value>; no changes made.` and exits successfully. The audit log records decisions that changed platform state, not how many times a runbook was pasted. |
| **Error handling** | Fail-closed. Every validation — `--test` vocabulary, reason length, UUID form, actor eligibility, restaurant existence — refuses with a `CommandError` before any write, and no audit row is produced for a refusal. |

Refusals are deliberately **not** audited, unlike the HTTP transition endpoint. There, a refusal is an authenticated administrator being told no. Here the commonest refusal is an actor that could not be resolved, so there is nobody to attribute the row to — and writing denial rows from an unauthenticated shell would let anyone with box access fill the audit log with attribution nobody stood behind.

To reverse a classification, run the command again with the opposite `--test` value and a reason saying why. Both directions are first-class and both are audited; the correction must not happen in a shell.

**No restaurant has been classified with this command yet.** It ships as a mechanism; running it against a specific tenant is a separate, explicit operational action taken after review and deploy.

#### `adopt_restaurant_onboarding`

| | |
|---|---|
| **Run** | `python manage.py adopt_restaurant_onboarding --restaurant <UUID> --actor <platform-staff-username> --reason "<why>"` |
| **Arguments** | All three are **required**. `--restaurant` is the restaurant's UUID. `--actor` is the username of the platform-staff human making the decision. `--reason` is a free-text justification of at least 10 characters after trimming (the same bar as a lifecycle transition, a delegation grant and `mark_restaurant_test`). |
| **What it does** | Represents **exactly one** pre-existing canonical `Restaurant` in the Admin onboarding domain by creating one `RestaurantOnboarding` row with `source = legacy_adopted`, `adopted_at` = the moment of the operation and `adopted_by` = the actor, and writing one `admin.restaurant.onboarding_adopted` audit row. Both share one `transaction.atomic()`, so a failed audit rolls the adoption back. |
| **Why it exists** | Step 2A created the onboarding domain with **no backfill** and nothing that writes to it, so absence of a row truthfully means "not yet represented in Admin". This is the first writer, and the one provenance a pre-existing tenant can honestly carry. Adoption means only *this restaurant is now represented in the Admin onboarding domain* — not that Dinify created it, not that anyone has vouched for its owner, and not that its owner was ever invited. |
| **Thin adapter** | The command adds no policy of its own. Every rule lives in `platform_admin_app.onboarding_adoption.adopt_existing_restaurant`, which the future Admin HTTP endpoint will call unchanged — so the shell and the portal can never adopt on different terms. |
| **Targeting** | **UUID only.** There is no `--restaurant-name`, no fuzzy match, no `.first()`, and no bulk mode. A malformed UUID, an unknown UUID and a soft-deleted restaurant are all refused, the last two with the same message so the command never confirms a soft-deleted tenant exists. Lifecycle state is **not** a blocker: a legacy restaurant may be `onboarding`, `live`, `suspended` or `offboarded` and still need truthful provenance. |
| **Actor** | Must exist, be `account_type = platform_staff`, and be active. Validated by the command *and* re-validated against the database row by the service, because the service is what writes the audit row. This is **attribution, not authentication**: anyone who can run `manage.py` on the box already has more authority than this command grants. No password, TOTP code or recovery code is asked for, and none is printed. |
| **Owner-consistency prerequisite** | A **NEW** adoption requires `platform_admin_app.onboarding.assert_owner_consistency` to pass under the lock: exactly one active, non-deleted owner-role `RestaurantEmployee`, and its user is `Restaurant.owner`. `missing_owner_membership`, `multiple_owner_memberships` and `owner_membership_mismatch` each refuse the adoption with that code and a next step. |
| **No automatic repair** | It never creates a missing owner membership, picks between two live owners, reassigns `Restaurant.owner`, deactivates a membership or rewrites roles. Ambiguous ownership is a decision about who runs a business; a human resolves it separately. |
| **Creates ONLY** | One `RestaurantOnboarding` row. **No** `OwnerInvitation` — a legacy tenant did not enter Dinify through the invitation system, and fabricating one would claim an invitation was issued, delivered and accepted. **No** owner-control attestation: all three of `owner_control_attested_at` / `_user` / `_by` are left NULL, because adoption is not a personal verification that the owner controls the account. That is a separate audited decision and is **not implemented**. No User, Restaurant, RestaurantEmployee, RestaurantRolePermission, subscription, payment config, readiness or approval row. |
| **Does NOT modify the Restaurant** | Not lifecycle state, not `is_test`, not the owner, not employees, menus, tables, QR state or historical orders. The service never calls `restaurant.save()`, so even the `auto_now` `time_last_updated` stamp is unchanged — a test asserts the whole row is byte-identical afterwards. |
| **Locking** | One `transaction.atomic()`; the first statement takes `select_for_update()` on the target `Restaurant`. That row is the serialization point, so two concurrent operators adopting the same tenant queue instead of racing to insert the one-to-one row. It takes **no** admission advisory lock, no table-allocation lock and no QR lock — adoption changes nothing an order path reads, and every extra lock is one a future transaction can deadlock against. Order: `Restaurant → RestaurantOnboarding → AdminAuditLog`. |
| **Source conflict** | If the restaurant already carries `admin_created` provenance the command **refuses** with `onboarding_source_conflict`, changes nothing and writes no audit row. `admin_created` names the staff member who created the tenant; converting it would delete that attribution and replace it with a contradictory claim. Two records disagreeing is something a human has to look at. |
| **External services** | PostgreSQL only. No SMS, no email, no MongoDB, no encryption key, no scheduled execution. |
| **Scheduling** | **None.** Like every command here it is invoked by hand; nothing in this repo runs it. |
| **Idempotency** | **Good, and specifically about history.** A restaurant already adopted as `legacy_adopted` gets a successful no-op with its **original** `adopted_at` and `adopted_by` intact and no second audit row — a different operator with a different reason a year later cannot rewrite who made the call or when. It also does **not** re-check owner consistency for an already-adopted restaurant: ownership can drift long afterwards, and that does not make the historical adoption untrue. Current consistency is a current-state question and is surfaced separately. |
| **Error handling** | Fail-closed. Reason length, UUID form, actor eligibility, restaurant existence, the provenance conflict and the owner-consistency precondition all refuse with a `CommandError` before any write. Narrow domain errors (`invalid_restaurant_id`, `restaurant_not_found`, `invalid_actor`, `invalid_reason`, `onboarding_source_conflict`) rather than a leaked `IntegrityError` / `DoesNotExist`. |

Refusals are deliberately **not** audited, for the same reason as `mark_restaurant_test`: the commonest refusal is an actor that could not be resolved, so there is nobody to attribute a row to.

**Baba House is NOT adopted merely because this command exists.** Nothing runs automatically, there is no backfill and no signal, and running it against a specific tenant is a separate, explicit operational action taken after review and deploy. Until then, the Admin read contract continues to report `claim_tracked: False` for every restaurant.

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
| `mark_restaurant_test` | platform_admin | PG | Good | Fail-closed | — |
| `adopt_restaurant_onboarding` | platform_admin | PG | Good | Fail-closed | — |

---

## Go-live is blocked until Phase 1 (deliberate)

`restaurants_app.controllers.lifecycle.check_go_live_readiness` **fails closed** as of PR-D: it returns not-ready with the single blocker `readiness_not_configured`, so the `onboarding → live` transition is refused on every path — the admin endpoint, the service, the shell.

It previously returned ready unconditionally without ever reading the restaurant it was passed. That is worse than having no gate: the one check standing between a half-built restaurant and real diners always said yes, while reading as protection.

**Nothing is stranded by this today.** There is no API path that creates a restaurant (PR-A retired the last one), and the single production restaurant is already `live`. So there is no restaurant that needs to go live and cannot.

**There is deliberately no override.** Not a flag, not a management command, not a service parameter. A bypass built "just until Phase 1" is exactly the kind that outlives its reason, and the thing being bypassed is the launch gate. Phase 1 replaces the function body — the seam's shape, its call site and its blocker vocabulary are already in place.

If a restaurant genuinely must go live before Phase 1 lands, that is a decision to make explicitly and with a code change, not an operational workaround.

---

## Operational Gaps

### No scheduling infrastructure

There is no Celery, Celery Beat, crontab, Procfile, or any other scheduler in this repository. All management commands are presumably scheduled externally (system cron, Kubernetes CronJobs, etc.), but that configuration is not documented or version-controlled here. If the external scheduler breaks or is misconfigured, there is no way to tell from this repo alone what should be running and when.

#### What the external scheduler actually was (discovered 2026-07-29)

The warning above was borne out. Read-only reconnaissance of the production host,
ahead of the Python 3.12 runtime migration, found what had been scheduling these
commands — and that it had been broken for months.

- The host ran **a single root `crontab` entry**, firing **every minute since
  2024-12-12**, invoking `/home/scripts/process_transactions.sh`.
- That wrapper called `manage.py process_transactions` against **two** paths: a
  `dev` checkout deleted in May 2026, and the live UAT checkout.
- `process_transactions` was deleted in the custodial teardown. **Both
  invocations had been failing on every run** — one with a missing path, one with
  `Unknown command` — accumulating **594 daily log files totalling 739 MB**.
- `/home/scripts/` held **eleven wrapper scripts**. Nine correspond to management
  commands that no longer exist. Two correspond to commands that do:
  `determine-customers` and `send_messages` — **neither of which is scheduled**.

The consequence, stated plainly: **no scheduled background work is currently
running.** The only cron entry that existed called a command that no longer
exists. If `send_messages` or `determine-customers` are meant to run on a
schedule, nothing is running them — unsent notifications are not being dispatched
and orders are not being matched to customers except when someone runs the
commands by hand.

This is **documented, not fixed.** Establishing what should be scheduled, and
scheduling it somewhere version-controlled, remains outstanding. The host-side
findings from the same reconnaissance are recorded in
[`REGULATORY_AUDIT.md`](REGULATORY_AUDIT.md) under *APPENDIX — Post-audit host
findings*.

### No retry behaviour

No command implements retry logic. If an external call fails (SMTP or the SMS gateway), the command crashes immediately and all remaining items in the batch are skipped. There are no dead-letter queues, no exponential backoff, no retry counters. Recovery depends entirely on re-running the command on the next scheduled invocation.

### No per-item error isolation

With the exception of `vacuum_deleted_records` (partial), every command that loops over items and calls external services will crash on the first failure. Items after the failure point are never processed until the next run.

### No monitoring or alerting

No command emits metrics, health checks, or alert signals. There are no Prometheus counters, no Datadog tags, no Sentry breadcrumbs. If a command fails silently (e.g. processes zero records because a filter matches nothing), there is no mechanism to detect this.

### No failure notifications

When a command fails, no email, Slack message, or other notification is sent. Operators must either check logs manually or rely on the external scheduler to report non-zero exit codes (if it is configured to do so).

### Logging

The `orders_app` / `notifications_app` / `misc_app` commands use `print()` instead of Python's `logging` module or Django's `self.stdout.write()`. This means:
- Output does not include timestamps, log levels, or structured fields
- Output cannot be routed to log aggregation systems without additional tooling
- There is no way to distinguish informational messages from errors in the output

The four `platform_admin_app` commands are the exception: they write through `self.stdout` and raise `CommandError` for failures, so a caller can separate the two streams and read the exit code. Their durable record is the `AdminAuditLog` row, not the terminal output.

### Idempotency gaps

- `send_messages` can re-send emails if the command crashes after sending but before marking as sent in MongoDB
