# D11 enforcement: durable abuse limits on public account endpoints

D11 B2-C collected evidence only. This change makes OTP issuance, and the public
`msisdn-lookup`, refuse once the limits below are reached. The limits hold across
mod_wsgi workers and processes because they live in PostgreSQL. The per-process DRF
throttles stay where they are, as defence in depth.

Issuances are counted and refused by **what the requester has proved**. As first merged
(#374) every code counted in one budget, so anyone who knew an owner's phone number could
spend that owner's login budget with anonymous reset requests, and a few addresses could
reach the global cap and refuse every login on the platform. The correction keeps the
owner's numbers and counts each class against them separately (see "The two classes").

D11 stays open after this: what the limits should be after launch, the operational
alert route (H5), and the residuals listed at the end are still the owner's.

## The limits

The owner's starting values (27 October 2026). Every count and window is a setting
with this default, read from the environment of the same name.

| # | limit | default | counted from | applies to | decided in |
|---|---|---|---|---|---|
| L1 | OTP issuance per subject (`u:<User>`, or `p1:<phone>` when there is no account) | 3 / 15 min and 10 / 24 h | `otp_issuances.subject_key`, rows of the issuance's own class | each class, separately | `make_otp` |
| L2 | OTP issuance per destination phone (`destination_key`), all subjects and purposes | 3 / 15 min and 10 / 24 h | `otp_issuances.destination_key`, rows of the issuance's own class | each class, separately | `make_otp` |
| L3 | OTP issuance per client IP, across reset initiation and every resend except the login resend (which is how registration sends its code) | 20 / hour | `account_abuse_counters`, scope `otp_issue` | unverified only | `make_otp` |
| L4 | global OTP issuance | 300 / 24 h, alert at 80% and at the cap | `otp_issuances`: the unverified rows for the refusal, every row for the alerts | refuses unverified only | `make_otp` |
| L5 | `GET users/msisdn-lookup/` per client IP | 30 / hour | `account_abuse_counters`, scope `msisdn_lookup` | every lookup | the endpoint |
| L6 | verify attempts per challenge | 5, unchanged | `user_otps.attempts` | every challenge | `verify_otp` |

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

One setting serves both classes: `OTP_LIMIT_SUBJECT_SHORT_COUNT=3` gives each class 3.

A count must be at least 1. A window must be between 60 seconds and 7 days: the ledger
keeps rows for 7 days, so a longer window would count rows that may already be gone.
The percentage must be 1 to 100, and the e-mail empty or one valid address. A value
outside that fails the Django system check, so `check --deploy` stops the deploy
before Apache restarts. If it is changed on the host without a deploy, the OTP routes
answer 500 rather than issue unlimited codes. There is no "off" value. To relax a
limit, raise it.

`ENV=dev` and its OTP `1234` are unchanged. The limits apply under `dev` too, because
they count issuances, and `dev` writes an issuance row for every challenge.

## The two classes

An issuance is **verified** when the requester proved something only the account holder
has before the code was issued. It is **unverified** when anyone could have asked.

The class is a function of the issuance's `origin` and nothing else
(`otp_limits.issuance_class`). The origin is server-owned: each production caller names
its own, `make_otp` maps any other value (or none) to `unattributed`, and the database
constrains the column to the vocabulary. The decision uses the same origin the ledger row
records, so an issuance is decided in the class it is later counted in. Nothing in the
request body, headers or address chooses it.

| origin | class | why | production call site |
|---|---|---|---|
| `password_login` | verified | issued only after the password is accepted | `users_app/controllers/login.py` (`login`) |
| `login_resend` | verified | E-R4: honoured only with the proof the password step returned | `users_app/controllers/otp_manager.py` (`_resend_login`) |
| `owner_claim_challenge` | verified | needs a valid claim token, about 288 bits | `platform_admin_app/endpoints/owner_claim.py` (`OwnerClaimChallengeView`) |
| `reset_initiation` | unverified | anyone who knows the number can ask | `users_app/controllers/reset_password.py` (`initiate_password_reset`) |
| `resend_request` | unverified | any other resend: registration's code, reset resends, null purpose | `users_app/controllers/otp_manager.py` (`resend_otp`, two calls) |
| `unattributed` | unverified | no origin, or one outside the vocabulary | none today |

The verified set is an ALLOWLIST. Any other origin, including one the vocabulary gains
later, is unverified until somebody decides otherwise. Every production `make_otp` call
names its origin, and none reaches the limits as `unattributed`;
`users_app/tests_otp_limits.py::OriginInventoryTests` scans the source and fails when a
call is added, removed or passes no origin.

**What each class gets.**

- **L1 and L2, per class.** An issuance counts only the ledger rows of its own class,
  against the unchanged numbers. Anonymous requests can never spend a verified budget,
  and verified ones can never spend an anonymous budget.
- **L3, unverified only.** A verified issuance has already passed a credential check.
  Applying L3 to it let a stranger behind the same carrier-grade NAT address, which is
  common on Ugandan mobile networks, refuse sign-ins for everyone sharing it. A verified
  issuance takes no client lock, reads no counter and writes no hit.
- **L4 refuses unverified issuances only, and its alerts count every issuance.** The
  refusal compares the unverified rows in the window with the cap. The alerts report
  real spend, verified codes included. Spend from the verified class needs working
  credentials, and L1 and L2 still bound it per account.
- **L5 and L6 are unchanged.**

The owner-claim challenge passes no client to `make_otp`. #374 recorded that as a gap
(L3 did not apply there) with a one-line follow-up in `platform_admin_app/`. That
follow-up is no longer needed: the challenge is verified, and L3 does not apply to a
verified issuance whichever client it comes from.

## Where each limit is decided, and the lock order

L1 to L4 are decided at one point: inside `make_otp`'s existing issuance transaction,
after the locks and before the replacement DELETE. The order is:

1. `users` row `FOR KEY SHARE` (user-backed challenges only; unchanged, B2-C)
2. the bucket advisory lock, class `OTPI` (unchanged, E-R3)
3. the subject advisory lock, class `OTPS` (`0x4F545053`), keyed by the subject key
4. the destination advisory lock, class `OTPD` (`0x4F545044`), keyed by the destination key
5. the client advisory lock, class `OTPC` (`0x4F545043`), keyed by scope and client key,
   **for an unverified issuance only**
6. the decision. One statement over `otp_issuances` returns every ledger count: the
   four keyed counts for the issuance's own class, every issuance in the L4 window (for
   the alerts) and the unverified issuances in it (for L4). For an unverified issuance,
   one statement then sums the client's counter buckets. The decision is made in Python
   after both.
7. on refusal: ROLLBACK (nothing was written) and the refusal below
8. otherwise as before: DELETE, challenge INSERT, ledger INSERT, then, for an unverified
   issuance, the client counter increment
9. COMMIT, then the alert (if any) in a thread, then delivery, then finalization

All three D11 locks use PostgreSQL's two-integer advisory form, with first keys of
their own, the same form as `OTPI` and apart from the admission locks' single-bigint
form. Each is transaction-scoped, so COMMIT or ROLLBACK releases it and nothing is held
across delivery. A key that cannot be derived (a subject or destination that cannot be
keyed, or a caller that is not acting for a network client) skips that lock and that
limit, and so does a verified issuance's client key.

**The subject and destination locks are the same for both classes.** They are keyed by
the subject and destination alone, never by class, so a verified and an unverified
issuer for one account still take turns, and each counts the other's committed row as
not its own class. Each class's count is therefore exact while the other class issues
too (`tests_otp_limits_concurrency.py::ClassesAreCountedApartTests`).

**Why this cannot deadlock.** Every issuer takes at most one lock of each class, always
in the order `OTPI`, `OTPS`, `OTPD`, `OTPC`; a verified issuer takes the first three. An
issuer holding a later class never waits for an earlier one. The lookup endpoint takes
only `OTPC`, under its own scope. Verify and owner-claim redemption take no advisory
lock. Redemption's `users FOR UPDATE` still meets issuance's `FOR KEY SHARE` first,
before issuance holds anything. Two subjects (or destinations, or clients) whose keys
share 32 bits take turns. That is false contention, not a wrong count.

### Overshoot

- **L1, L2 and L3 are exact.** They are serialized by `OTPS`, `OTPD` and `OTPC`. The review
  allowed either locks or a stated overshoot. Locks are the choice because the budget is
  per victim. An attacker who fires many simultaneous reset requests for one number
  would otherwise push one budget over by as many requests as reach the database at
  once, which is the attack L1 and L2 exist to stop. In normal use the locks cost
  nothing: different people do not share them.
- **L4 has a bounded overshoot, and it is deliberate.** An exact global count needs one
  global serialization point, and every unverified issuance on the platform would then
  take turns, refusals included. Without it, issuers that run their decision before each
  other's COMMIT can each see the unverified count one below the cap and each be
  admitted. The overshoot is at most one issuance per unverified issuance transaction in
  flight between its decision and its COMMIT. That window is a few statements long, with
  no I/O. The bound is therefore the customer plane's concurrent request capacity, a host
  fact this repository does not set. The test pins two concurrent issuers at `cap - 1`
  both admitted, and the next one refused.
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

| route | class | on a limit refusal | why |
|---|---|---|---|
| `POST auth/login/` (owner/manager OTP branch) | verified | the 429 (L1, L2) | decided only after the password was accepted, where the answer already differs from a wrong password (200 vs 401) |
| `POST auth/resend-otp/`, `purpose=login` | verified | the 429 (L1, L2) | the E-R4 proof is checked first; every refusal before it is E-R4's own |
| `POST auth/resend-otp/`, every other admitted purpose | unverified | the 429 (L1 to L4) | |
| `POST auth/initiate-reset-password/` and the legacy `reset-password` without `otp` | unverified | **E-R1's acknowledgement, unchanged**, and no code issued | the established answer is uniform, so it stays uniform |
| `POST owner-claim/challenge/` | verified | the 429 (L1, L2) | `make_otp` raises a DRF `APIException`, which DRF renders; `platform_admin_app/` is not changed |
| `GET msisdn-lookup/` | — | the 429 (L5) | checked before the lookup, so it is the same for a number with or without an account |
| `POST auth/register/`, `verify-otp`, reset completion | — | unchanged | they issue nothing; L6 applies |

A refusal issues no challenge, deletes nothing, writes no `otp_issuances` row,
increments no counter and sends nothing.

`make_otp`'s contract changes in one way: on a limit refusal it raises
`otp_limits.OtpIssuanceLimited`, an `APIException` with status 429 and the body above.
It does not return `False`. A caller that forgets to catch it, inside a DRF view, still
answers the right 429. The users_app callers catch it and answer as the table says.

### What the timing can and cannot hide

It can hide which limit fired. Every refusal of one class on one route runs the same
statements: the locks, the one ledger statement and, where L3 applies (the unverified
class), the one counter statement, then the decision in Python. A verified refusal can
only be L1 or L2, both decided from the ledger statement, and runs no counter statement,
as #374 already did for a caller that passes no client. The alert work for an L4 refusal
happens in a thread, so it adds nothing to the request. Tests pin that the statements are
the same for an L1, an L2, an L3 and an L4 refusal of a sign-up code, and for an L1 and an
L2 refusal of a password login.

It cannot hide:

- that the request was refused rather than admitted. An admitted request writes rows
  and, under `test`/`prod`, waits up to 3 s for the SMS. The status already says this.
- lock waits. A request that waits behind another issuer for the same subject,
  destination or (unverified) client is slower. A sign-in no longer waits behind an
  unverified issuer from its own address; it can still wait a few statements behind any
  issuer for the same account or phone.
- the per-process DRF throttles. They answer a different 429 (`{"detail": …}` with
  `Retry-After`) before any of this runs.
- E-R1's existing timing residual on reset initiation, which is unchanged.

## Existence: what a limit can and cannot reveal

The body of a refusal never depends on whether an account exists, and neither does the
class: it comes from the route and the purpose the caller chose, which the caller already
knows, never from an account fact. A resend that resolves an account and one that does
not are both `resend_request`; a reset initiation is `reset_initiation` whether or not it
issues.

But whether a refusal happens depends on earlier issuances OF THE SAME CLASS, and reset
initiation issues only for an eligible account. So a sequence of anonymous requests can
learn eligibility. For example, three reset initiations for a number, then one generic
resend to it: the resend is refused only if the resets issued. That fact is already
public. `msisdn-lookup` answers it in one request, by design, now limited by L5.
`resend-otp` with `identification=phone` answers it directly (400 "User not found"), an
E-R1 residual. So the limits add a slower channel to information already disclosed.

Counting per class closes one channel #374 had. There, the account holder's own sign-ins
spent the budget an anonymous resend is decided against, so an anonymous resend refused
after three sign-ins told anyone asking that the account had just signed in. A sign-in is
verified now, and no anonymous answer depends on it.

No route whose answer is uniform today starts answering differently per account in a
single request: reset initiation still acknowledges every refusal, and the only routes
whose class is verified (login after the password, the login resend after its proof, the
owner-claim challenge after its token) already answered per account before any limit.

## Alerts (L4)

- **Warning.** It fires when an admitted issuance, of either class, brings the rolling
  24-hour count of EVERY issuance to at least `ceil(cap × OTP_SPEND_ALERT_PERCENT / 100)`
  (240 by default). It is about what codes cost, so verified codes count.
- **Cap.** It fires when L4 refuses an issuance, which only an unverified issuance can be.
- **Once per 24 hours.** Each alert is claimed by one conditional upsert on
  `otp_spend_alerts`. The claim succeeds when no claim exists, or the last one is older
  than 24 hours. So each alert fires at most once per rolling 24 hours, across every
  worker.
- **What a claim does.**
  - It logs one bounded line with a fixed marker, unchanged from #374:
    `OTP_SPEND_ALERT threshold=<warning|cap> issued=<n> cap=<n> email=<sending|not_configured>`.
    `issued` is every issuance in the window, verified ones included, so on a cap alert
    it can be above the cap.
  - It sends one e-mail to `OTP_SPEND_ALERT_EMAIL` through
    `notifications_app.controllers.messenger.Messenger.send_email`. The e-mail states
    every issuance and how many were unverified, and says the cap refuses only those.
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
- **Verified spend past the cap raises no cap alert.** The warning fires at 240 of every
  issuance; the cap alert fires only when an unverified request is refused.

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
  `hits` and `expires_at`. Only unverified issuances (and lookups) write it. Expired rows
  are deleted in bounded batches after each admitted issuance and each admitted lookup.
- **`otp_spend_alerts`** has one row per alert, `last_sent_at`.
- **The class is not stored.** It is derived from `otp_issuances.origin`, which B2-C
  already records, so the correction needed no migration.

## The migration

`users_app/0016_d11_enforcement`: two `CreateModel`s and nothing else. No `RunPython`,
no backfill, and no existing table is altered or indexed. `CREATE TABLE` takes no lock
on any existing table. The per-class correction adds no migration.

- **The decision statement needs no new index.** It reads `otp_issuances` through the
  existing `created_at` index, over the longest window: every issuance of the last 24
  hours. The class filters are evaluated on the rows that scan already fetches. L4 bounds
  the unverified rows (the cap plus its overshoot); L1 bounds the verified rows to 10 a
  day per account that receives a verified code. Measured on PostgreSQL 18.3 against
  20,000 ledger rows over 7 days (2,916 in the last 24 hours): the statement before and
  after the correction has the same plan (a bitmap index scan on
  `otp_issuance_created_idx`, one aggregate) and touches the same 500 buffers.
- **Rollback** (a code-only redeploy of an older commit, as the deploy does) leaves both
  tables and their rows in place. The old code neither reads nor writes them, so they
  are ignored and nothing is restored. The limits simply stop. Reversing the migration
  would drop both tables. The deploy never does that. Rolling back only the per-class
  correction (to #374) restores #374's single budget, with the two consequences above.

## What this does not protect against

- **Distributed sources.** L3 and L5 are per IP. A botnet, or many residential or mobile
  addresses, gets L3 and L5 again for every address. L1, L2 and L4 still hold.
- **Shared addresses, for anonymous flows.** Carrier-grade NAT is common on Ugandan
  mobile networks, and many unrelated people can share one public IPv4 address. They
  share its L3 and L5 budgets, so a busy or abusive neighbour can refuse their password
  resets, registration codes and lookups for up to an hour. Their sign-ins and owner
  claims are not affected.
- **SIM farms and number pools.** Each fresh number has its own L1 and L2. Only L3 and
  L4 bound them.
- **A flood with credentials.** Verified issuances are bounded per account by L1 and L2,
  not by L3 or L4. Someone holding the passwords of many accounts (or a claim token) can
  issue up to 10 codes a day for each. The spend alerts still count those codes.
- **An anonymous flood still blocks anonymous flows.** Once the unverified cap is
  reached, every password reset, registration code and other unverified resend on the
  platform is refused until the oldest anonymous codes age out: for up to 24 hours. And
  anyone who knows a phone number can spend that account's anonymous L1 and L2 budget
  (3 per 15 minutes, 10 per 24 hours), so its holder cannot get a password-reset code for
  up to 24 hours. Sign-ins are not affected by either.
- **Up to double the per-account SMS volume before a refusal.** Each class has the
  numbers above, so one account or phone can receive 6 codes in 15 minutes and 20 in a
  day before both budgets are spent, at most half of them anonymous.
- **The SMS provider's own limits and billing.** These limits count issuances, not what
  the gateway charged or delivered.
- **Password guessing at login.** No limit here counts login attempts. Only the
  per-process `auth_login` throttle bounds them.
- **Workers on the previous build during a rollout.** They take none of these locks and
  enforce none of these limits, or (from #374 to this correction) enforce the single
  budget.

## What UAT will see with the defaults

UAT runs `ENV=dev`, so codes are `1234` and nothing is sent by SMS. The limits still
apply.

- **The served journey check.** It signs one operator in about once per run, a few runs
  a day. Each run is one verified issuance for that account and its phone. The check
  stays inside the defaults while that account receives no more than 3 sign-in codes in
  15 minutes and 10 in 24 hours. Manual sign-ins with the same operator share that
  budget; password resets and resends to that account do not.
- **People testing by hand.** An owner or manager who signs in more than 3 times in 15
  minutes, or more than 10 times in a day, gets "Too many attempts. Please wait and try
  again." until the oldest code ages out. Repeated resets, registration codes or resends
  for one account or number have their own budget of the same size, and from one
  address the anonymous ones also share 20 an hour.
- **An owner claim after a lockout.** Every claim challenge is a verified issuance to
  the invited owner, so it counts towards that owner's verified L1 and L2, with their
  sign-ins. A reissued invitation starts a fresh claim-attempt budget, but it does not
  reset these limits: an owner who requested 3 claim codes in 15 minutes waits for the
  oldest to age out before the new invitation can be challenged.
- The UAT host can raise any limit in its `.env`.
