# D11 enforcement: durable abuse limits on public account endpoints

D11 B2-C collected evidence only. This change makes OTP issuance, and the public
`msisdn-lookup`, refuse once the limits below are reached. The limits hold across
mod_wsgi workers and processes because they live in PostgreSQL. The per-process DRF
throttles stay where they are, as defence in depth.

D11 stays open after this: what the limits should be after launch, the operational
alert route (H5), and the residuals listed at the end are still the owner's.

## The limits

The owner's starting values (27 October 2026). Every count and window is a setting
with this default, read from the environment of the same name.

| # | limit | default | counted from | decided in |
|---|---|---|---|---|
| L1 | OTP issuance per subject (`u:<User>`, or `p1:<phone>` when there is no account) | 3 / 15 min and 10 / 24 h | `otp_issuances.subject_key` | `make_otp` |
| L2 | OTP issuance per destination phone (`destination_key`), all subjects and purposes | 3 / 15 min and 10 / 24 h | `otp_issuances.destination_key` | `make_otp` |
| L3 | OTP issuance per client IP, across login, resend (which is how registration sends its code), reset initiation and owner-claim challenge | 20 / hour | `account_abuse_counters`, scope `otp_issue` | `make_otp` |
| L4 | global OTP issuance | 300 / 24 h, alert at 80% and at the cap | `otp_issuances` | `make_otp` |
| L5 | `GET users/msisdn-lookup/` per client IP | 30 / hour | `account_abuse_counters`, scope `msisdn_lookup` | the endpoint |
| L6 | verify attempts per challenge | 5, unchanged | `user_otps.attempts` | `verify_otp` |

Settings (all integers unless noted):

```
OTP_LIMIT_SUBJECT_SHORT_COUNT=3       OTP_LIMIT_SUBJECT_SHORT_SECONDS=900
OTP_LIMIT_SUBJECT_LONG_COUNT=10       OTP_LIMIT_SUBJECT_LONG_SECONDS=86400
OTP_LIMIT_DESTINATION_SHORT_COUNT=3   OTP_LIMIT_DESTINATION_SHORT_SECONDS=900
OTP_LIMIT_DESTINATION_LONG_COUNT=10   OTP_LIMIT_DESTINATION_LONG_SECONDS=86400
OTP_LIMIT_CLIENT_COUNT=20             OTP_LIMIT_CLIENT_SECONDS=3600
OTP_LIMIT_GLOBAL_COUNT=300            OTP_LIMIT_GLOBAL_SECONDS=86400
OTP_SPEND_ALERT_PERCENT=80
OTP_SPEND_ALERT_EMAIL=                (string; empty means no e-mail)
MSISDN_LOOKUP_LIMIT_CLIENT_COUNT=30   MSISDN_LOOKUP_LIMIT_CLIENT_SECONDS=3600
```

A count must be at least 1. A window must be between 60 seconds and 7 days: the ledger
keeps rows for 7 days, so a longer window would count rows that may already be gone.
The percentage must be 1 to 100, and the e-mail empty or one valid address. A value
outside that fails the Django system check, so `check --deploy` stops the deploy
before Apache restarts. If it is changed on the host without a deploy, the OTP routes
answer 500 rather than issue unlimited codes. There is no "off" value. To relax a
limit, raise it.

`ENV=dev` and its OTP `1234` are unchanged. The limits apply under `dev` too, because
they count issuances, and `dev` writes an issuance row for every challenge.

## Where each limit is decided, and the lock order

L1 to L4 are decided at one point: inside `make_otp`'s existing issuance transaction,
after the locks and before the replacement DELETE. The order is:

1. `users` row `FOR KEY SHARE` (user-backed challenges only; unchanged, B2-C)
2. the bucket advisory lock, class `OTPI` (unchanged, E-R3)
3. **new:** the subject advisory lock, class `OTPS` (`0x4F545053`), keyed by the subject key
4. **new:** the destination advisory lock, class `OTPD` (`0x4F545044`), keyed by the destination key
5. **new:** the client advisory lock, class `OTPC` (`0x4F545043`), keyed by scope and client key
6. **new:** the decision. One statement over `otp_issuances` returns all five ledger
   counts, and one statement sums the client's counter buckets. Both always run, and
   the decision is made in Python after both.
7. on refusal: ROLLBACK (nothing was written) and the refusal below
8. otherwise as before: DELETE, challenge INSERT, ledger INSERT, then **new:** the
   client counter increment
9. COMMIT, then the alert (if any) in a thread, then delivery, then finalization

All three new locks use PostgreSQL's two-integer advisory form, with first keys of
their own, the same form as `OTPI` and apart from the admission locks' single-bigint
form. Each is transaction-scoped, so COMMIT or ROLLBACK releases it and nothing is held
across delivery. A key that cannot be derived (a subject or destination that cannot be
keyed, or a caller that is not acting for a network client) skips that lock and that
limit.

**Why this cannot deadlock.** Every issuer takes at most one lock of each class, always
in the order `OTPI`, `OTPS`, `OTPD`, `OTPC`. An issuer holding a later class never waits
for an earlier one. The lookup endpoint takes only `OTPC`, under its own scope. Verify
and owner-claim redemption take no advisory lock. Redemption's `users FOR UPDATE` still
meets issuance's `FOR KEY SHARE` first, before issuance holds anything. Two subjects (or
destinations, or clients) whose keys share 32 bits take turns. That is false contention,
not a wrong count.

### Overshoot

- **L1, L2 and L3 are exact.** They are serialized by `OTPS`, `OTPD` and `OTPC`. The review
  allowed either locks or a stated overshoot. Locks are the choice because the budget is
  per victim. An attacker who fires many simultaneous reset requests for one number
  would otherwise push one budget over by as many requests as reach the database at
  once, which is the attack L1 and L2 exist to stop. In normal use the locks cost
  nothing: different people do not share them.
- **L4 has a bounded overshoot, and it is deliberate.** An exact global count needs one
  global serialization point, and every issuance on the platform would then take turns,
  refusals included. Without it, issuers that run their decision before each other's
  COMMIT can each see the count one below the cap and each be admitted. The overshoot is
  at most one issuance per issuance transaction in flight between its decision and its
  COMMIT. That window is a few statements long, with no I/O. The bound is therefore the
  customer plane's concurrent request capacity, a host fact this repository does not
  set. The test pins two concurrent issuers at `cap - 1` both admitted, and the next one
  refused.
- The alerts do not depend on exactness: each is claimed by one conditional statement on
  `otp_spend_alerts`.

## Windows

- **L1, L2 and L4 are true rolling windows.** A row counts while `created_at > now() -
  window`, both on the database clock (`STATEMENT_TIMESTAMP()`), so a row stops counting
  exactly `window` after it was written.
- **L3 and L5 use a rolling approximation with a strict upper bound.** Each window is cut
  into 12 aligned buckets (5 minutes for an hour). A request is admitted when the hits in
  the current bucket and the 12 before it are below the limit.
  - That coverage always includes the last full window. So in any rolling window of
    that length, admissions never exceed the limit.
  - The cost is that a hit is forgotten between 60 and 65 minutes after it happened,
    not at exactly 60.
  - Buckets use the application host's clock. Both planes run on one host.

## The refusal, per route

All limit refusals share one body. It carries no `Retry-After` header and no reason,
because either would say which window fired:

```
HTTP 429  {"status": 429, "message": "Too many attempts. Please wait and try again."}
```

| route | on a limit refusal | why |
|---|---|---|
| `POST auth/login/` (owner/manager OTP branch) | the 429 | decided only after the password was accepted, where the answer already differs from a wrong password (200 vs 401) |
| `POST auth/resend-otp/` (every admitted purpose, the login resend included) | the 429 | |
| `POST auth/initiate-reset-password/` and the legacy `reset-password` without `otp` | **E-R1's acknowledgement, unchanged**, and no code issued | the established answer is uniform, so it stays uniform |
| `POST owner-claim/challenge/` | the 429 | `make_otp` raises a DRF `APIException`, which DRF renders; `platform_admin_app/` is not changed |
| `GET msisdn-lookup/` | the 429 | checked before the lookup, so it is the same for a number with or without an account |
| `POST auth/register/`, `verify-otp`, reset completion | unchanged | they issue nothing; L6 applies |

A refusal issues no challenge, deletes nothing, writes no `otp_issuances` row,
increments no counter and sends nothing.

`make_otp`'s contract changes in one way: on a limit refusal it raises
`otp_limits.OtpIssuanceLimited`, an `APIException` with status 429 and the body above.
It does not return `False`. A caller that forgets to catch it, inside a DRF view, still
answers the right 429. The users_app callers catch it and answer as the table says.

### What the timing can and cannot hide

It can hide which limit fired. Every refusal on a route runs the same statements: the
locks, the one ledger statement and the one counter statement, then the decision in
Python. The alert work for an L4 refusal happens in a thread, so it adds nothing to the
request. A test pins that the statements are the same for an L1, an L3 and an L4
refusal.

It cannot hide:

- that the request was refused rather than admitted. An admitted request writes rows
  and, under `test`/`prod`, waits up to 3 s for the SMS. The status already says this.
- lock waits. A request that waits behind another issuer for the same subject,
  destination or client is slower.
- the per-process DRF throttles. They answer a different 429 (`{"detail": …}` with
  `Retry-After`) before any of this runs.
- E-R1's existing timing residual on reset initiation, which is unchanged.

## Existence: what a limit can and cannot reveal

The body of a refusal never depends on whether an account exists. But whether a refusal
happens depends on earlier issuances, and reset initiation issues only for an eligible
account. So a sequence of requests can learn eligibility. For example, three reset
initiations for a number, then one generic resend to it: the resend is refused only if
the resets issued.

That fact is already public. `msisdn-lookup` answers it in one request, by design, now
limited by L5. `resend-otp` with `identification=phone` answers it directly (400 "User
not found"), an E-R1 residual. So the limits add a slower channel to information already
disclosed. They disclose nothing new. No route whose answer is uniform today starts
answering differently per account in a single request.

## Alerts (L4)

- **Warning.** It fires when an admitted issuance brings the rolling 24-hour count to at
  least `ceil(cap × OTP_SPEND_ALERT_PERCENT / 100)` (240 by default).
- **Cap.** It fires when L4 refuses an issuance.
- **Once per 24 hours.** Each alert is claimed by one conditional upsert on
  `otp_spend_alerts`. The claim succeeds when no claim exists, or the last one is older
  than 24 hours. So each alert fires at most once per rolling 24 hours, across every
  worker.
- **What a claim does.**
  - It logs one bounded line with a fixed marker:
    `OTP_SPEND_ALERT threshold=<warning|cap> issued=<n> cap=<n> email=<queued|not_configured>`.
  - It sends one e-mail to `OTP_SPEND_ALERT_EMAIL` through
    `notifications_app.controllers.messenger.Messenger.send_email`.
  - The claim and the send run in a daemon thread, off the request path. A process
    allows at most one alert thread per alert per minute, so a flood of refusals cannot
    start a flood of threads.
- **If the send fails.** The thread logs `OTP_SPEND_ALERT_EMAIL_FAILED threshold=<…>`.
  The claim stands, so the next attempt is 24 hours later.
- **If `OTP_SPEND_ALERT_EMAIL` is unset.** No e-mail is sent. The log line is still
  written once per alert per 24 hours.
- **The e-mail reaches a person only if the host's e-mail path delivers.** H5 recorded
  the live box with `EMAIL_HOST=localhost`. Whether anything on that host relays mail is
  not established here.

## Keys and storage

- **The client key.** `client_ip_from_request` gives the address: REMOTE_ADDR on the
  verified one-hop topology. The key is `ip1:` + HMAC-SHA256 under the existing OTP
  pepper, over `dinify:abuse-limits:client:v1` + the address. The raw IP is never stored.
  - An IPv6 address is keyed by its /64. One subscriber usually holds a whole /64, so
    keying the full address would let one host step through 2^64 keys.
  - An IPv4-mapped IPv6 address is treated as IPv4.
  - A missing or unparseable address shares one key, which fails closed together.
  - A pepper rotation changes every client key, which resets the client counters.
- **`account_abuse_counters`** has one row per `(scope, client_key, bucket_start)`, with
  `hits` and `expires_at`. Expired rows are deleted in bounded batches after each
  admitted increment.
- **`otp_spend_alerts`** has one row per alert, `last_sent_at`.

## The migration

`users_app/0016_d11_enforcement`: two `CreateModel`s and nothing else. No `RunPython`,
no backfill, and no existing table is altered or indexed. `CREATE TABLE` takes no lock
on any existing table.

- **The decision statement needs no new index.** It reads `otp_issuances` through the
  existing `created_at` index, over the longest window. That is every issuance of the
  last 24 hours, which L4 bounds.
- **Rollback** (a code-only redeploy of an older commit, as the deploy does) leaves both
  tables and their rows in place. The old code neither reads nor writes them, so they
  are ignored and nothing is restored. The limits simply stop. Reversing the migration
  would drop both tables. The deploy never does that.

## What this does not protect against

- **Distributed sources.** L3 and L5 are per IP. A botnet, or many residential or mobile
  addresses, gets L3 and L5 again for every address. L1, L2 and L4 still hold.
- **Shared addresses.** Carrier-grade NAT is common on Ugandan mobile networks, and
  many unrelated people can share one public IPv4 address. Legitimate users behind one
  address share its L3 and L5 budgets. They can be refused when someone else on that
  address is busy, or abusive.
- **SIM farms and number pools.** Each fresh number has its own L1 and L2. Only L3 and
  L4 bound them.
- **A victim locked out by a stranger.** L1 and L2 span purposes. Anyone who knows a
  phone number can spend that account's budget with anonymous reset initiations (3 per
  15 minutes, 10 per 24 hours). The owner of that account then cannot get a login code
  until the oldest issuance ages out: for up to 24 hours if all 10 are spent. This
  follows from the decided policy, and it is the owner's call whether to accept it.
- **The SMS provider's own limits and billing.** These limits count issuances, not what
  the gateway charged or delivered.
- **Password guessing at login.** No limit here counts login attempts. Only the
  per-process `auth_login` throttle bounds them.
- **The owner-claim challenge under L3.** That route is in `platform_admin_app/`, outside
  this change. It does not pass the client, so L3 does not apply there. Reaching it at
  all requires a valid claim token (about 288 bits), and L1, L2 and L4 still apply. The
  follow-up is one line: pass `client=otp_limits.Client.from_request(request)` to
  `make_otp`.
- **Workers on the previous build during a rollout.** They take none of these locks and
  enforce none of these limits.

## What UAT will see with the defaults

UAT runs `ENV=dev`, so codes are `1234` and nothing is sent by SMS. The limits still
apply.

- **The served journey check.** It signs one operator in about once per run, a few runs
  a day. Each run is one login issuance for that account, its phone and the runner's IP.
  The check stays inside the defaults while that account receives no more than 3 codes
  in 15 minutes and 10 in 24 hours. That budget is shared with every other code that
  account or its phone receives on UAT, so manual sign-ins and password resets with the
  same operator count too.
- **People testing by hand.** An owner or manager who signs in more than 3 times in 15
  minutes, or more than 10 times in a day, gets "Too many attempts. Please wait and try
  again." until the oldest code ages out. The same goes for repeated resends or resets
  for one account or one number.
- The UAT host can raise any limit in its `.env`.
