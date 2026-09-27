# Backend release candidate — D08 B2.5, its preflight — D08 B2.6, and its installation — D08 B3

**One retained candidate whose exact source and complete Python dependency files are the
ones its identified CI run validated and audited, and an independent proof that those
retained inputs alone rebuild the environment.** This directory is the producer, the
consumer, the preflight (B2.6 — see [The preflight](#the-preflight-d08-b26)) and the
rules they apply.

It does **not** deploy anything. `deploy-uat.yml` is untouched: it still checks out the
commit on the box and runs `pip install -q -r requirements.txt` into the shared serving
venv, so **the live host has not acquired the guarantee this candidate carries** (a test
pins that statement, so it cannot quietly become false). The preflight does not change
that either: it runs beside the live deploy, a red preflight does not stop it, and it
cannot protect it. Connecting promotion to the candidate and a received preflight is B3.

**B3 builds that connection and does not switch it on** — see
[The installation](#the-installation-d08-b3). The installer, the transition and the
loaded-process identity are implemented and were rehearsed on a disposable host; the
workflow and host script that would drive them on the real host are STAGED under
`release/staged/`, where no workflow can reach them; and the committed UAT host profile is
`unverified`, so the real mutating path refuses it by name. `deploy-uat.yml` is still the one
live writer.

## What is certified

| | where it comes from | what binds it |
|---|---|---|
| **Direct inputs** | `requirements.txt`, byte-unchanged, 24 `name==version` lines | its sha256, recorded in the lock |
| **The closure** | `python-lock.json` — reviewed, committed | its sha256; every file's exact URL, size and sha256 |
| **Package files** | the lock's exact `files.pythonhosted.org` addresses | admitted only on an exact size + sha256 match |
| **Installer** | pip 26.2.1, a separate `bootstrap` entry in the lock | its own wheel hash; it installs itself from that wheel |
| **Environment** | built offline from the wheelhouse alone | reconciled against the lock and every wheel's own `RECORD` |
| **Source** | `git archive` of the commit, never the working tree | the git tree id, recomputed from the archive |
| **Validation** | every step of the `suite` leg, run IN that environment | the steps' own recorded outcomes |
| **Audit** | the B2.1 audit of that environment's inventory | the retained raw output, re-decided at packaging |

The target is **CPython 3.12.3, Linux x86_64, glibc 2.39** (ubuntu-24.04, the image CI now
pins). Lock entries are refused unless a wheel for that target can run them: a
manylinux tag needing a newer glibc, another ABI, an sdist or a platform wheel for
another OS is `wheel_incompatible` or `lock_invalid`.

The closure is **26 application packages** — the 24 direct inputs plus `cffi 2.1.1`
(required by `cryptography`) and `pycparser 3.0` (required by `cffi`) — plus **pip
26.2.1** as the installer: 27 files, 28,540,311 bytes. Installer, application and the
tool that resolved the lock are labelled apart in the lock; the scanner is a fourth
identity, pinned separately in `dependency_audit/scanner-requirements.txt`.

## The candidate

```
backend-candidate[-nonpromotable]-<run>-<attempt>/
  record.json          dinify.backend.candidate/1 — the binding (no digest of itself)
  source.tar           git archive of the commit
  wheelhouse/*.whl     exactly the lock's 27 files
  evidence/*           the 9 dependency-audit files of THIS run
```

`record.json` binds: repository, commit, tree; the run context (workflow ref and sha,
event, ref, run id, attempt, number, job, runner image); target facts; the installer and
its exact flags; the direct-input and lock digests; the archive's sha256, size, file
count, content digest and tree; every wheel's name, version, sha256 and size and the
listing digest; the reconciled installed inventory, its portable digest, `pip check` and
the markers; every required step's outcome; the audit's policy and scanner-requirements
digests, scanner version, outcome, counts, snapshot/collection/decision timestamps,
re-decision and evidence listing digest; and three continuity observations (before
validation, after validation, after packaging). The artifact's own ID and digest are
established later from the run's artifact listing — an artifact cannot contain its own
final hash.

**Eligibility.** Only a push to `refs/heads/main` in `mugak1/Dinify-Backend` produces a
*promotable* candidate, named `backend-candidate-<run>-<attempt>`. A pull-request run
validates a merge preview, so it produces `backend-candidate-nonpromotable-…`. Promotable
means eligible to be **considered** by a later promotion-time assessment. It authorizes
nothing.

**What is never in it.** `.env*` (the committed `.env.example` template aside), databases,
private keys and certificates, `uploads/`, virtualenvs, `node_modules`, bytecode, audit
evidence and wheel paths are **refused, not filtered**: the archive is the whole commit,
and a forbidden path or recognisable key material in it (PEM private keys, AWS access-key
ids) stops packaging and is reported by path only, never by value. The committed tree
contains none of them today, and a test keeps it that way.

## The producer (the `suite` leg of `ci.yml`)

1. `lock check` — the lock is canonical, internally consistent, target-compatible and
   the resolution of the committed `requirements.txt`. **A changed `requirements.txt`
   with the old lock is `stale_lock`** — even a comment edit, because the digest is of
   the file. Certification never regenerates a lock.
2. `observe` — the checkout IS the commit: every tracked file's bytes and executable
   bit equal the blob HEAD records (so a `skip-worktree` or `assume-unchanged` edit that
   `git status` hides is caught), and nothing untracked exists.
3. `acquire` — NETWORK, PyPI's file host only. Each locked file from its exact address,
   admitted on size + sha256; one retry; nothing is resolved or chosen.
4. `install` — OFFLINE. `python -m venv --without-pip`; the pinned pip runs from its own
   wheel and installs itself; the closure installs with `--isolated --no-input
   --disable-pip-version-check --no-cache-dir --no-index --find-links <wheelhouse>
   --require-hashes --no-deps --only-binary=:all:`, under a scrubbed environment and a
   dead proxy. Then **reconciliation**: the installed set equals the lock; the
   `Requires-Dist` closure from the direct inputs reaches every application package and
   nothing else; every wheel's `RECORD` matches the wheel's own bytes and every installed
   file matches the wheel; no unowned file sits in `site-packages`; `pip check` passes.
   The environment's `bin/` then leads `PATH`.
5. `snapshot` + `interpreter` — the B2.1 inventory snapshot of THAT environment, and a
   check that the `python` every later step runs is the certified one with nothing added
   to its import path.
6. Every existing gate, unchanged: migrations, the three guards, guard qualification,
   the audit evaluator tests, **the release tests**, the tenant gate, the full suite, the
   dependency audit.
7. `package` — runs only when all of the above succeeded (no `if:`), and re-checks it
   from `toJSON(steps)`. Re-observes the checkout (after validation, only bytecode beside
   tracked `.py`, the audit's `evidence/` and test `uploads/` may exist, and none of them
   importable); re-verifies the wheelhouse; re-reconciles the environment; re-decides the
   audit from its retained raw output and requires it bound to this commit, this
   `requirements.txt` and exactly the locked inventory; exports the archive and requires
   it to hash to the tree; copies and re-verifies; observes once more; then writes the
   record. Any problem removes the partial output.
8. Upload — `if-no-files-found: error`, 30-day retention.

## The consumer (the `reconstruct` job)

A separate job on a fresh runner, `contents: read`, no credential, `needs: suite`. It
checks out **its own** copy of `release/` and `dependency_audit/` at the expected commit
and receives the candidate as DATA. In order, and nothing in the candidate runs until the
last group:

1. Container shape — exactly the four entries, no links, no stray files.
2. Identity — schema, repository, commit, tree, run, attempt, event, ref, artifact name,
   workflow path and eligibility against the consumer's **own** expectations (the run's
   `GITHUB_*` context), never against the record's claims about itself.
3. Archive — sha256 and size against the record; member rules (no absolute or escaping
   paths, links, devices, duplicates, late headers, empty directories); **the git tree id
   recomputed from the archive must equal the tree of the expected commit read from the
   consumer's own git**; content digest; forbidden paths and key material.
4. Lock and `requirements.txt` read from the verified archive, checked as in step 1 of
   the producer, and bound to the record.
5. Wheelhouse — exactly the lock's files, sizes and hashes, and the record's listing.
6. Audit evidence — the nine files against the record, and **re-decided from the raw
   scanner output** with the B2.1 evaluator, bound to this commit and inventory.
7. Only then: refuse if an `.env` exists above the work directory (Django's settings
   search upward); extract; rebuild offline in a fresh venv exactly as the producer did;
   reconcile; require the **portable environment digest to equal the record's** and the
   target facts to match.
8. Bounded startup, with `-I` under the reconstructed interpreter and disposable
   settings (SQLite in memory, no network): the customer plane (`test_settings`,
   Django's `check`, the WSGI application, `GET /api/v1/health/`) and the admin plane (a
   generated settings module over `settings_admin` with the same disposable database,
   `check`, `wsgi_admin`, `GET /admin/v1/health/` for `admin.dinifyapp.com`); every locked
   distribution's top-level modules import; native smoke checks (psycopg's C
   implementation, Pillow, `cryptography`'s Fernet, qrcode).

The reconstruction report is retained as `backend-reconstruction-<run>-<attempt>`. The
original record and its timestamps are never rewritten. `test` now requires **both** the
leg and the reconstruction; a skipped reconstruction is not success.

## The preflight (D08 B2.6)

**Given one exact retained candidate: establish from outside it that its CI run certified
it, ask the advisory question again now over exactly the inventory it retains, and leave a
small, time-limited result that a later installer can check for itself.** It deploys
nothing, observes no host and changes nothing. Every result it writes says
`deploymentAuthorized: false`. A passing preflight is a statement about evidence at a
recorded time. It is **not** production readiness.

`release/preflight.py`, three commands, run by `.github/workflows/preflight.yml`:

| command | token | what it does |
|---|---|---|
| `preflight facts` | read | Resolves the request, reads the certification facts from the GitHub API and downloads the candidate and reconstruction zips **by artifact id**, as bytes, unopened. |
| `preflight assess` | none | Selects again from those facts, admits each zip by the listing's digest, verifies the candidate, binds the reconstruction, then queries advisories now and decides under the trusted policy. Writes the result. |
| `preflight verify` | none | The receiving side. Its own facts, the result from the evaluation run's own listing, the decision reproduced from the raw output. |

### Certification is read from GitHub, never from the candidate

A record's `promotable: true` is eligibility metadata, and the candidate is uploaded by the
`suite` leg **before** `reconstruct` and `test` run. So the preflight requires all of the
following from the API:

- **The workflow.** It must be the one whose path is `.github/workflows/ci.yml`, by
  workflow id and path, never display name.
- **The run.** A completed, successful push to `main` for the exact commit. The run's
  repository and head repository must be `mugak1/Dinify-Backend`.
- **Ancestry.** The commit is on `main` (compare: `ahead` or `identical`, with merge base
  equal to the commit), and its tree is the git tree the API states.
- **The jobs.** The selected attempt's own job listing is complete. It holds
  `suite (3.12.3)`, `reconstruct` and `test` exactly once each, all completed
  successfully, all carrying that attempt, run and commit. `REQUIRED_JOBS` is held equal
  to the job names in the committed `ci.yml` by a test.
- **The artifacts.** The run's artifact listing is complete. It holds exactly one
  unexpired `backend-candidate-<run>-<attempt>` and exactly one
  `backend-reconstruction-<run>-<attempt>` of that attempt, each with an id, a
  `sha256:` digest, a size and the run's head commit. A non-promotable candidate name for
  that attempt is a contradiction and is refused.

**Partial re-runs cannot mix attempts.** "Re-run failed jobs" produces an attempt whose
listing carries jobs from the earlier attempt. Any job not run by the selected attempt is
`certification_mixed_attempt`. The fix is to re-run **all** jobs, so one attempt carries
the candidate and every check that judged it.

**Selection.** Automatic runs use the triggering run and attempt and nothing else. Manual
runs take an exact commit, and optionally a run and attempt:

- no run given: the only successful push-to-main `ci.yml` run for that commit is used;
  more than one is `certification_ambiguous`;
- a run without an attempt: that run's latest attempt is used.

The request comes from the event through the environment and is validated. Nothing
event-derived is interpolated into a script.

### The bytes are the certified bytes

Each zip is admitted only if its sha256 is the listing's digest, **before** anything is
extracted. The member rules are then applied: no absolute or escaping paths, links,
devices, encryption or duplicates, and bounded sizes. Extraction uses `O_EXCL|O_NOFOLLOW`
into an empty directory.

The candidate then goes through the B2.5 consumer's `verify`, with the expected commit,
tree, run, attempt, event, ref, artifact name and workflow path taken **from the
selection**. Nothing in it executes: no candidate module is imported and no installer,
script or hook runs.

The reconstruction report must be the one file `reconstruction.json` and must say
verified, with no problems and a started application. It must be bound to exactly this
record's sha256, artifact, commit, tree, run and attempt, and to this record's
environment digest.

### The fresh query

**The application inventory is the RETAINED one.** It is the exact `name==version` set in
the candidate's certification snapshot. It is refused unless its B2.1 inventory digest
equals the record's `environment.auditInventorySha256`. It is classified by the
**trusted** scope rule and queried with the pinned pip-audit as exact pins:
`--no-deps --disable-pip --strict`. Nothing is installed, resolved or built. The
environment certification installed is not re-observed (those bytes are gone), and the
result says `observation: retained-inventory` rather than implying otherwise.

**The scanner comes from the trusted checkout.** It is installed now from
`dependency_audit/scanner-requirements.txt`, hash-pinned, from the verifier's own
checkout. Its inventory is checked against the pins, queried like the application's
(`observation: installed-now`), and inspected again after its scan.

**Isolation.** Every process on the scanner path gets the B2.1 scrubbed environment plus
`PIP_CONFIG_FILE=/dev/null`, because `--isolated` alone still reads the global and site
`pip.conf` (measured below). It also runs without the runner's step-output files,
`*_TOKEN` or `ACTIONS_*`. A test plants a `pip.conf` with a `find-links` and a dead proxy:
the B2.1 CI install is still steered by it, and this bootstrap is not.

**No earlier answer.** Each graph gets a fresh, empty `--cache-dir` that is removed
afterwards.

**Real timestamps.** Each graph records the actual start and finish of its query. The CLI
has no clock override and no scanner override.

**THE TRUSTED POLICY DECIDES.** The decision is `dependency_audit/policy.json` from the
verifier's own checkout, identified by the sha256 of its bytes. It is never the policy
inside the candidate. The candidate's original audit is still verified by the consumer, as
**history**: it stays in the result under `candidate.originalAudit`, labelled, never
rewritten or re-dated. A certification-time exception that has lapsed, or that the trusted
policy no longer carries, does not survive into the fresh decision. An unreadable,
partial or failed answer is `incomplete`, never clean.

**Freshness.** The deadline is 24 hours from the **start of the evaluation**, which is
earlier than the first query, so the window is never generous. Any record the decision
applied cuts it short at that record's lapse, and a record whose lapse cannot be read gives
no deadline at all.

The trusted `release/` and `dependency_audit/` trees must equal both the evaluator
revision's and **current main's**. A result from an evaluator that main has since moved
past is `evaluator_not_current`, and the remedy is a new evaluation, never a refresh.

### The result — `dinify.backend.preflight/1`

`preflight.json` beside the raw evidence (`<graph>.inventory-requirements.txt`,
`<graph>.scanner-stdout.txt`, `<graph>.scanner-stderr.txt` for both graphs). It binds:

- the `scope` block (`kind: non-deploying preflight`, `deploymentAuthorized: false`,
  `hostObserved: false`, `hostMutated: false`);
- the request; repository, commit and tree;
- the certification: workflow id and path, run, attempt, event, head branch and start, and
  each required job's id;
- the candidate and reconstruction artifacts by id, name, digest, size and creation time;
- the candidate's record, archive, tree, content, wheelhouse, lock, requirements,
  environment and audited-inventory digests, and its original audit (history);
- the reconstruction report's sha256 and environment digest;
- the evaluator: run, attempt, revision, trusted trees, policy sha256, scanner-requirements
  sha256, scanner version and install, scanner inventory digest;
- per graph: the observation kind, query start and finish, inventory and requirements
  digests, the recorded run (argv, exit, raw output digests) and the package list;
- the outcome, counts, headline, reasons, applied records, window and **deadline**.

`decision` is `accepted` (within policy or exceptions only), `blocking`, `incomplete`, or
`refused` (a selection, candidate or binding problem stopped it before the query). The
result is retained as `backend-preflight-<run>-<attempt>` whatever it decided.

### The receiving check — the contract for B3

`preflight verify` trusts nothing it cannot reproduce. It reads its **own** facts and
selects again. It then finds the result by name in the evaluation run's own artifact
listing, bound to that run's head commit. The assessing job's reported id and digest are
hints that must agree. `upload-artifact` reports its digest as bare hex while the
listing says `sha256:<hex>`, and `listing_digest()` reads both spellings as one digest.
Comparing them verbatim refused every valid result; Codex caught it on PR #343. A hint
given at all must be whole: a numeric id and a digest. An empty output means the upload
report is missing, which is `request_invalid`, never "no hint". The result zip is admitted
by the listing's digest, and the receiving side then refuses unless all of the following
hold:

- the scope is exactly the non-deploying scope, the decision is `accepted`, and the file
  set is exact;
- repository, commit, tree, certifying run, attempt, jobs, workflow and both artifacts are
  the selection's;
- the candidate facts equal what this side derives from the candidate's bytes through the
  consumer again, and the reconstruction report's sha256 matches;
- the evaluator run, attempt, workflow path and revision are the evaluation run's;
- the trusted trees are this verifier's and main's (`preflight_evaluator_changed`);
- the policy sha256 is this side's trusted policy's (`preflight_policy_changed`);
- the scanner requirements and version are this side's trusted pins
  (`preflight_wrong_scanner`);
- the raw files hash to what the result recorded (`preflight_raw_mismatch`);
- the application query was over exactly the retained inventory, and the scanner query
  over exactly the trusted pinned set (`preflight_wrong_inventory` / `preflight_wrong_scanner`);
- **the decision reproduces from the raw output under this side's trusted policy**
  (`preflight_unreproducible`);
- **time**:
  - every recorded time is present and ordered;
  - the evaluation began no earlier than the evaluation run started;
  - the decision is no later than GitHub's own record of the result's upload, and not in
    the future;
  - a 2-minute tolerance applies, only between the runner's clock and GitHub's;
  - the deadline is recomputed and must equal the result's;
  - it must be **more than 30 minutes away** (`preflight_expired`). `--margin-minutes`
    can raise that margin, never lower it; a lower value is a usage error.

It writes only bounded, validated values as outputs: commit, candidate artifact id and
digest, environment digest and the deadline epoch. **Nothing in this repository consumes
them yet.** The installer that must repeat this check at its own boundary is B3.

### The workflow

`preflight.yml` has two jobs, `assess` and `verify`. Each makes its own trusted sparse
checkout (`release/`, `dependency_audit/`) at the workflow's own revision, persists no git
credential, and asserts that it holds none. Top-level `permissions: {}`, and each job has
`contents: read` and `actions: read` only.

- **Deploys nothing.** There is no id-token, stored secret, AWS, SSM, S3 or deploy step,
  and a test keeps it that way.
- **The token reaches one step per job.** `GH_TOKEN` is in the "Read the facts" step only,
  which runs no scanner. The assess and verify steps have no token, and assess removes
  runner authority before anything runs.
- **Triggers.** It runs after a successful `Backend CI` push to main (`workflow_run`),
  or on `workflow_dispatch` with `sha`, and optional `ci_run_id` and `ci_run_attempt`.
  A CI run that did not succeed produces a **skipped** preflight. Once an evaluation
  begins, every refusal, blocking finding or incomplete scan is red, and the result is
  still retained for 30 days.
- **Bounded.** Timeouts are 30 and 20 minutes, with 900 s on the facts read.
- **Pinned.** Every action is pinned by commit, and a test keeps it that way.
  `checkout` v4.4.0 and `setup-python` v5.6.0 are the commits `ci.yml`'s floating `@v4`
  and `@v5` resolve to today. `upload-artifact` is `ci.yml`'s reviewed pin.
- **Not a gate on the live deploy.** `deploy-uat.yml` is unchanged and consumes none of
  this. A red preflight does not stop it, and the first-merge effect of this change is
  only that a preflight runs after the next successful main CI. Backend CI itself becomes
  stricter only through the added tests. The release-tests step picks up
  `tests_preflight.py` and `qualify_preflight.py`, and the qualification adds about
  90 seconds.

Exit status for `preflight assess`: **0** accepted · **1** refused or blocking · **2**
incomplete · **64** usage. `facts` and `verify` are 0 or 1 (and 64).

### Measured at delivery (2026-09-26) — what was real and what was not

**Real facts.** The real public-API facts for main's certifying run `36261585225` attempt
1 (commit `9a6a7e8`, tree `f8b9ba78`) select cleanly, with:
- jobs `suite (3.12.3)` `108458322038`, `reconstruct` `108460588526`, `test`
  `108460716245`;
- candidate artifact `10912927026` (`sha256:3acbbf64…50c7`);
- reconstruction `10912347747` (`sha256:742b1ae0…5258`).

Three controls change one fact each and are refused:

| change | refusal |
|---|---|
| reconstruct marked failed | `certification_job_not_successful` |
| suite job carried from another attempt | `certification_mixed_attempt` |
| truncated listing | `certification_listing_incomplete` |

**That artifact's bytes could not be fetched.** GitHub redirects artifact zips to its blob
storage, and this environment's egress policy refuses that host (403). This was not routed
around.

**A genuine local candidate of the same commit stood in for it:**
- every suite-leg step was run for real, on setup-python's own CPython 3.12.3 build. The
  deviations: PostgreSQL 16 instead of CI's 15, and a synthetic run id. 4,883 Django tests
  passed;
- it was packaged by the real producer and reconstructed by the real consumer;
- its portable environment digest, `191df5a2…80213`, is **identical** to the one the real
  run's reconstruction reported.

**A real `preflight assess` over it (this change as the evaluator):**
- pip-audit 2.10.1 was installed from PyPI by hash, with the isolation above;
- real queries went to PyPI's vulnerability service: 27 retained application packages and
  29 scanner packages, no advisories, `within_policy`;
- about 17 seconds end to end.

**A real `preflight verify` received it** and reproduced the decision. Controls on that
same result:

| change | refusal |
|---|---|
| received 20 minutes before its deadline | `preflight_expired` |
| re-dated three hours and re-listed under a matching digest | `preflight_time_invalid` |

**A positive control:** the same isolated scanner path asked about `django==4.2.0` gets 50
advisories back from PyPI and blocks.

**Synthetic in that run: the provenance only.** The run id `9100000001`, the artifact ids,
the evaluation run `7100000001`, and "main" (the evaluator revision, since this branch is
unmerged). They are written in exactly the shape `preflight facts` leaves them.

## The installation (D08 B3)

**The candidate CI certified, installed as an immutable release on the host, switched onto
both WSGI planes deliberately, and verified by asking the RUNNING processes what they
loaded.** Four states are kept apart, and only the first two are claimed here:

| state | where it stands |
|---|---|
| implementation tested | **yes** — `tests_host.py`, `tests_staged.py`, `misc_app/tests_release_identity.py`, and the rehearsal below |
| runtime profile verified | **no** — `release/profiles/uat-backend.json` is `unverified`; every host fact is `null` |
| cutover authorized | **no** — `release/CUTOVER.md` lists every owner approval, none given |
| candidate serving | **no** — the live host serves whatever `deploy-uat.yml` last put there |

### What merging this does, and what it does not

- `deploy-uat.yml` is **unchanged** and still triggers on every successful Backend CI run on
  main. It is the only active writer. Nothing under `.github/workflows/` names the staged
  files, and `tests_staged.py` fails the build if both deploy workflows ever exist at once.
- Two **new unauthenticated routes** go live through that legacy deploy:
  `GET /uat/api/v1/release/` (customer) and `GET /api/admin/v1/release/` (admin). Under the
  legacy in-place install they answer `unavailable` / `not_started_by_release_launcher` —
  true, bounded, non-secret, `no-store`, and exactly what the staged ordering guard reads as
  "legacy". They read no request data and touch no database.
- Nothing is installed on, copied to or read from the real host by this change.

### The pieces

| module | what it owns |
|---|---|
| `dinify_backend/release_identity.py` + `misc_app/endpoints/release_identity.py` | the loaded-process identity document (`dinify.backend.runtime-identity/1`), set ONCE at worker start and never re-read |
| `release/hostprofile.py` | the host profile (`dinify.backend.host-profile/1`): every host fact the installer relies on, with `null` meaning *nobody has observed it* |
| `release/installation.py` | admission (the B2.6 receiving check repeated on the host), preparation (build as the unprivileged preparer, reconcile, seal), the launcher and settings files each release carries |
| `release/transition.py` | the host lock, the journal, the configuration and migration gates, the switch, verification, restoration, resume, legacy adoption and recovery |
| `release/hostprobe.py`, `release/startup.py` | the probes the transition runs AS the runtime and migration identities, and the start-both-planes check at construction |
| `release/migration-decisions.json` | reviewed migration decisions, read from the TRUSTED verifier, never the candidate |
| `release/staged/` | NOT ACTIVE: the workflow template, the SSM host script, the ordering guard, the marker reader, the read-only discovery collector |

### The installed release

```
<releaseRoot>/<commit>-<16 hex>/          root-owned, nothing group/other-writable, never edited
    source/      the certified tree, extracted from the candidate's archive and re-hashed
    venv/        created AT THIS PATH from the host's base python and the retained wheels, offline
    wheelhouse/  the candidate's wheels, re-verified against the lock
    static/      collected here: static files are release-owned
    wsgi/        the launcher, the runtime module, one settings module per plane
    receipt.json dinify.backend.installed-release/1
```

- **The id is derived, never chosen**: the commit, then 16 hex of
  `sha256(environmentDigest:runtimeDigest)`. Two installs of one commit with different launcher
  files are two releases.
- **Built as the preparation identity** (a no-login account, not the runtime and not the
  migrator), offline (`--no-index`, `PIP_CONFIG_FILE=/dev/null`, a scrubbed environment),
  then **reconciled** exactly as B2.5 reconciles, then **sealed** root-owned. The runtime
  identity can read it and write nothing in it.
- **The interpreter is the host's own**: the venv is created from `profile.basePython`, and
  the profile pins its version, SOABI, and the sha256 of `libpython` and `mod_wsgi.so`. It is
  **not** claimed to be the interpreter CI ran — CI runs setup-python's upstream build, the
  host runs Ubuntu's.
- **Configuration and media live outside every release**: one configuration file per plane
  (`/etc/dinify-backend/<plane>.env`, root-owned, group-readable by the runtime), read by the
  runtime module with python-decouple's own reader; `MEDIA_ROOT` is `profile.media.root`.
  `host_problems` refuses an environment file anywhere at or above the release root, because
  python-decouple searches upward.
- **Reuse is verified, never assumed**: an existing directory is reused only if its receipt
  names it, re-derives its id, names the same record, commit and tree, and every file,
  owner and mode still matches. Anything else is `installed_release_mismatch` —
  refused, never repaired or overwritten. A partial directory not started by this operation is
  `partial_release_foreign` and left for an operator. **Nothing prunes releases.**

### The loaded-process identity

The launcher resolves its own real path to one release, reads that release's receipt, and
checks that the receipt names this directory and this commit, that `sys.prefix` is the
release's venv and that the imported `dinify_backend` lives in the release's source. Only then
is the identity `verified`; anything else is `mismatch` with a reason
(`receipt_unreadable`, `receipt_names_another_release`, `interpreter_outside_release`,
`source_outside_release`). It is computed once, at worker start: **a file changed later cannot
relabel a running process** — measured below, running workers kept reporting their release
over a tampered receipt, and fresh workers over the same receipt reported `mismatch`. A
process no launcher started (the legacy install) reports `unavailable`.

### The transition

```
host lock (bounded 300 s wait, never stolen) -> journal: locked
  recheck the host, the admission's deadline, the installed release, identity support
  gate     each plane's configuration probed AS the runtime identity   -> gated
  plan     migrations read AS the migration identity, decided against the trusted decisions
  migrate  only a reviewed expand plan, before any switch               -> migrated
  switch   both includes replaced (previous bytes kept), configtest, ONE reload -> switching, switched
  verify   identity, processes (cwd + mapped files), health on both planes    -> verified
  restore  on any failure after the switch                             -> verification-failed, restored
```

- **One include per plane** (`<includeDir>/dinify-backend-<plane>.conf`) is the only file
  written outside the release root and the state directory. It pins `python-home`, `home` and
  `python-path` to one release, so an old worker keeps its old paths while new workers start
  on the new ones. Vhosts are never edited.
- **The configuration gate** loads each plane's real settings AS the runtime identity and
  refuses, before anything moves: a file it cannot read, a file the runtime could write or
  with group-write or any permission for others, settings that fail to import (which is how
  `DINER_CAP_KEY` already fails closed), `DEBUG`, an `ENV` outside dev/test/prod, `ENV=dev`
  unless the profile states a reason for the deterministic test OTP, `ENV=test`/`prod` without
  the SMS provider keys, an admin `ADMIN_SECRET_ENCRYPTION_KEY` that is not a Fernet key, any
  deployment system check at ERROR, a database that does not answer `SELECT 1`, a
  `MEDIA_ROOT` that is not the profile's (or lies inside the release root, or cannot be
  written), and a `STATIC_ROOT` that is not the release's own. Values are never printed, and
  no email, SMS, OTP or payment request is ever made to test readiness.
- **Migrations are decided, not attempted.** Every pending migration needs a reviewed entry
  (exact file sha256, exact operation types, `expand`, the reviewing PR). Unreviewed,
  contracting, stale or contradicted plans stop the automated path (`migration_unreviewed`
  and siblings). A rollback to an older release across newer migrations is allowed only when
  every such migration is a reviewed `expand` — the older code will run on that schema.
- **The journal** is `<stateDir>/operations/<op>.jsonl`, one fsynced line per stage, plus
  `active.json` naming the one open operation. A new transition refuses while one is open
  (`previous_operation_unresolved`); `host resume` settles it by OBSERVING what serves. One
  thing it does not settle by observation: an operation that began applying migrations and
  never recorded that they completed stays OPEN (`schema_state_unknown`) even when what serves
  verifies, because re-reading the plan cannot describe a non-atomic migration that stopped
  half-way. Only an operator's statement, `--schema-established "<what was found>"` (at least
  20 characters, recorded verbatim), closes it; the statement is refused where no migration is
  unresolved, so it cannot become a habit.
- **The trusted verifier must be traversable.** The preparation, migration and runtime
  identities import it and run from inside it, so every directory on the way to it grants
  execute to others (`0711`: passable, not listable); `host deploy` refuses otherwise
  (`trusted_not_traversable`) and `host-run.sh` requires exactly `0711` for its base and
  `trusted/`, `0700` for `incoming/`. The rehearsal's F23/F25 ran the earlier `0700` check and
  stopped at the profile refusal, before any unprivileged step, so it could not see this; the
  review of the PR did.
- **The host lock** is shared with Dinify-Admin's host procedure by contract
  (`release/HOST_LOCK_CONTRACT.md`); the Admin half is staged there, not applied.

### Stages and exit codes

`python -B -m release host …`: **0** verified / unchanged / adopted · **1** refused (nothing a
request can reach changed) · **3** the new release failed and the previous one was restored
and re-verified · **4** restoration failed or the state is unknown (the operation stays open)
· **64** usage. The host prints its attestations first — `B3-ADMITTED`, `B3-PREPARED`,
`B3-OUTCOME` (exactly one, including on a refusal before anything moved), and one
`B3-SERVING` per plane — because SSM keeps only the first 24,000 characters.

### Three recoveries, kept apart

- **Certified rollback** — a normal transition to an older certified commit, which needs its
  retained candidate and a FRESH preflight. The installed directory is reused only after every
  byte is re-verified.
- **Restoration** — automatic, inside a failed transition: the kept previous includes, reload,
  re-verification. A red run that says `restored` means the new release is not serving.
- **Legacy recovery** — `host recover-legacy` puts back the directives recorded by
  `host adopt-legacy`. It is reported as legacy: the processes are healthy, and which code they
  loaded cannot be established.

### The handover — from source to a running process

| link | where it is recorded | what binds it to the previous link |
|---|---|---|
| source | the commit and its tree | git |
| candidate | `record.json` (`dinify.backend.candidate/1`) in `backend-candidate-<run>-<attempt>` | the archive re-hashed to the tree; CI's own job results read from GitHub |
| preflight | `dinify.backend.preflight/1` | the candidate's listed digest and record sha256 |
| admission | the receiving check's values (`preflight verify --admission-out`) | re-derived on the host by `ins.admit` from the same two zips, with the 30-minute margin |
| installed release | `<releaseRoot>/<id>/receipt.json` | commit, tree, record sha256, environment digest; the id re-derived from them |
| WSGI | `<includeDir>/dinify-backend-<plane>.conf`, header `# dinify-backend-release: <id> operation: <op>` | real paths into that one release |
| observed identity | `GET …/release/` from every sample, plus each daemon's cwd and mapped files | the running process's own launcher, checked against its own receipt |

### The rehearsal — what was real and what was modelled

All of it ran on a **disposable host inside this container** (Ubuntu 24.04, Apache 2.4 +
mod_wsgi 5.0 for Python 3.12, PostgreSQL 16, Python 3.12.3), never the real host.

**Real:** Apache, mod_wsgi daemon mode with two planes, graceful reloads, PostgreSQL and every
migration, the unprivileged preparation/migration/runtime identities, the offline build,
reconciliation, sealing, the identity endpoint answered by real workers, `/proc` evidence, the
journal, the kernel lock, the kill, the clock. The candidates' bytes were real: **R1** used
main's real CI candidate (artifact `10917740297`, `sha256:18f61328…5d35`), its real
reconstruction (`10917745336`) and its real preflight (`10917139266`, deadline
`2026-09-27T22:35:15.028Z` — historical, not an authorization); **R2** used three local
candidates (A `fe9b308`, B `7c570a0` adding migration `misc_app.0005_rehearsalnote`, C
`971797a` whose admin health can be broken by a file) — each certified by a local
reproduction of the suite leg (every step run, ~4,900 Django tests) and admitted through the
real `preflight assess` (real scanner, real PyPI queries) and real `preflight verify`.

**Modelled:** the GitHub provenance of the R2 candidates (run ids, artifact ids, listings), the
profile's `observation` block (a placeholder the rehearsal validator requires; nothing was
reviewed), the migration decision for `0005` (in a disposable trusted snapshot, never
committed), the loopback vhosts standing in for the real ones, an `apachectl` wrapper used for
one fault, and — for the staged host script only — an AWS CLI stub copying from a local
directory. No real host, account, bucket, role, secret or database was touched.

| # | scenario | outcome | evidence |
|---|---|---|---|
| R1 | main's real candidate `7e16d4b` admitted and prepared | installed; promotion **refused** `identity_unsupported` — it predates B3 | correct: nothing could verify what it loaded |
| R1 | a partial directory from another operation | **refused** `partial_release_foreign`, left in place | |
| F02 | adopt the legacy directives | `adopted` | |
| F03 | the committed UAT profile | **refused**: `profile_unknown` (34 fields named) + `profile_unverified` | |
| F04 | legacy → A | `verified`; both planes report A; daemons' cwd in A | 35 s incl. build |
| F05 | B with an unreviewed migration | **refused** `migration_unreviewed`; A kept serving; `0005` not applied | |
| F06 | B with a reviewed expand migration, behind a 60 s lock held by the **staged Admin snippet** run verbatim | waited (holder released 01:02:13.843, `locked` 01:02:14), migrated, `verified`; a 2.5 s request started at `switching` completed on A (`marker A`, 200) | |
| F07 | C with the space floor above free space | **refused** `insufficient_space`; nothing created | |
| F08a | C with its admin health broken before construction | **refused** at construction (`startup_failed`); nothing installed | |
| F08b | C broken only after preparation | switched, `verification-failed`, **`restored`** to B and re-verified; exit 3; C retained | |
| F09 | the same, with the second graceful reload failing | `restoration-failed`, exit 4: includes back on B, processes still on C, operation open | the honest split state |
| F10 | any deploy while that is open | **refused** `previous_operation_unresolved` | |
| F12 | `resume` | observed C loaded, refused to accept it, restored B, closed | exit 3 |
| F13a | rollback B → A across reviewed `0005` | `verified` (the harness's kill missed — it matched its own shell — recorded as such) | |
| F13 | forward A → B, `kill -9` at `switched` | operation left open at `switched`; the kernel released the lock | |
| F14 | `resume` | observed B, verified, closed | |
| F15 | B again | `unchanged`, the same four daemon pids before and after | |
| F16–F19 | admin key not Fernet · config unreadable by the runtime · config mode 644 · `ENV=dev` | each **refused** at `locked → refused`, nothing switched | |
| F20 | serving release's receipt edited | running workers still report B; a new deploy **refused** `installed_release_mismatch` | |
| F21 | fresh workers over the edited receipt | `mismatch` / `receipt_names_another_release`; restored → `verified` | |
| F22 | `recover-legacy` | legacy serving, login route answers 405 | |
| F23/F25 | `release/staged/host-run.sh` executed with placeholders substituted, stub AWS CLI | **refused** the committed profile; after the fix below, attests `B3-OUTCOME: refused`, which the marker reader maps to exit 1 | found a defect |
| F24/F26 | a wrong transfer digest · a reused operation id | **refused** before any Python ran | |
| — | `release/staged/discover_host.py` on the rehearsal host | ran read-only; no configuration value in its report | |
| — | every output file, state file and report searched for the six configuration secret values | **0 matches** | |

**What the rehearsal found and fixed** (each with a regression test): the probe's plane was
read before it was assigned (a refusal, legacy kept serving); an unchanged outcome attested
one release for both planes, which the staged reader would have called DEGRADED; a reused
directory whose receipt had been edited was reported reused, now the receipt must re-derive
its id; and a mutating action refused before any change printed no outcome, which the staged
reader would have called unknown.

**Measured, and worth knowing before the cutover**: a graceful reload reclaimed the previous
mod_wsgi daemons about **3 seconds** after the signal regardless of `shutdown-timeout=15`
(in R2, SIGUSR1 at 00:46:21.97, the old worker cut at 00:46:24.98, the client saw a 500). A
request shorter than that finishes on the old release. The legacy deploy's
`systemctl restart apache2` cuts every in-flight request at once.

### The staged integration — NOT ACTIVE

`release/staged/deploy-backend.yml` would run after "Backend Candidate Preflight" (or by
dispatch with an exact sha, preflight run and attempt): a credential-free `receive` job
re-reads the facts, repeats `preflight verify`, applies the ordering guard (the served
identities over the public origins: both planes legacy proceeds as the first transition; a
target that descends from, or equals, the served commit proceeds — the host then answers
`unchanged` or installs a differently certified candidate; a target behind it is SKIPPED
unless the dispatch says `rollback`; a split, mixed or divergent state refuses) and uploads
the admission; the `deploy` job
(the only one holding `id-token: write`) re-derives all of it, refuses a changed hint, stages
the four files by sha256, sends `host-run.sh` through SSM and reads the markers
(`markers.py`). The host script runs **nothing from the candidate** — the trusted verifier is a
`git archive` of `release/` and `dependency_audit/` at the workflow's own revision — and uses
the committed profile, so today it refuses. `release/CUTOVER.md` is the ordered plan; every
step that touches the real host, AWS, Apache or secrets is marked OWNER.

### What this does not prove

- **Anything about the real host.** Its Apache layout, interpreter, mod_wsgi build, accounts,
  configuration and disk are unobserved; `release/staged/discover_host.py` is how they will be
  (read-only, reviewed, OWNER-approved), and the profile stays `unverified` until then.
- **The installer's build path in CI.** `ins.build` is exercised on the rehearsal host (root,
  real identities); CI runs the pure rules and the refusals, not a construction.
- **A byte-identical interpreter** (as above), and **zero downtime** (the ~3 s drain).
- **GitHub, SSM, S3 and OIDC.** The staged workflow is parsed and its steps and scripts are
  executed locally by `tests_staged.py`; nothing reached those services.

## Exit codes

`python -B -m release …`: **0** accepted · **1** refused (every problem is printed with a
stable code) · **64** usage. There is no "incomplete but OK": `preflight assess` reports
an incomplete scan as **2**, and it is red.

## Regenerating the lock (a reviewed change, never CI)

```
python3.12 -B -m release lock generate --work /tmp/lockgen    # NETWORK: index + file host
git diff release/python-lock.json                             # review every line
```

`generate` resolves `requirements.txt` with the pinned pip's `--dry-run --report` from a
bootstrap venv, downloads every selected file, checks it against the resolver's hash,
reads the dependency edges from the wheels themselves and writes the canonical lock. It
must run on the target (3.12.3, x86_64, glibc ≥ the highest manylinux tag selected). A
direct-input change is **always** a lock change in the same PR: the stale-lock refusal
makes the order impossible to get wrong.

## Run it locally

```
PY=python3.12    # must be CPython 3.12.3 on x86_64 Linux for decisive results
$PY -B -m release lock check
$PY -B -m release observe --out /tmp/c/before.json
$PY -B -m release acquire --wheelhouse /tmp/c/wh
$PY -B -m release install --wheelhouse /tmp/c/wh --venv /tmp/c/venv --work /tmp/c/install
export PATH=/tmp/c/venv/bin:$PATH
python -m dependency_audit snapshot
python -B -m release interpreter --venv /tmp/c/venv
…                                    # the gates, with DATABASE_* pointing at Postgres
python -m dependency_audit self-test && python -m dependency_audit audit
python -B -m release package --local --step-outcomes '<json>' --wheelhouse /tmp/c/wh \
    --venv /tmp/c/venv --evidence dependency_audit/evidence --before /tmp/c/before.json --out /tmp/c/cand
python -B -m release reconstruct --local --candidate /tmp/c/cand --work /tmp/c/rebuild \
    --report /tmp/c/rebuild.json --expect-commit "$(git rev-parse HEAD)"
```

A local candidate is `backend-candidate-local` and is never promotable. Commit nothing
while this runs: the snapshot and the continuity checks bind to the revision.

## What this does not prove

- **What Apache serves.** The candidate is not installed anywhere; mod_wsgi, the host's
  Ubuntu-built Python, its system libraries and `.env` are outside it.
- **A byte-identical interpreter.** `actions/setup-python` installs an upstream CPython
  3.12.3; the host runs Ubuntu's package under the same version. The environment digest
  covers the installed packages and target facts, not the interpreter binary.
- **A trusted or reproducible build of the wheels.** They are PyPI's files, pinned by
  hash. Who built them, and from what, is not established here.
- **Timestamps are claims.** They are the runner's clock, recorded, never trusted as
  ordering. The audit collection records one timestamp for its start and finish (the
  B2.1 evaluator's existing behaviour, reproduced here, not rewritten).
- **The actions and runner image.** Not audited (as in B2.1).

## A measured pip behaviour worth knowing

`pip install --isolated` ignores `PIP_*` environment variables and the user's config file
but **still reads `PIP_CONFIG_FILE`, the global `/etc/pip.conf` and a site `pip.conf`**.
Measured here: a `find-links` offered through a configuration file satisfied an install
that `--no-index` alone should have refused. The certified install sets
`PIP_CONFIG_FILE=/dev/null` — pip's documented switch for loading no file — and a test
fails if it is removed. The preflight's scanner bootstrap sets it too (B2.6), and
`qualify_preflight.py` proves the difference with real pip. It plants a site `pip.conf`
carrying a `find-links` and a dead proxy: the unchanged B2.1 CI scanner install is steered
by it, and the preflight's is not. The B2.1 CI scanner install keeps that exposure
(`--require-hashes` still bounds it to the same hashed files); it is recorded in
`dependency_audit/README.md` and left unchanged, because it is a CI scanner-policy change.

## Tests

`tests_*.py` (fast, run by `unittest discover -p "tests_*.py"`): the lock rules, the
source and archive rules, and the workflow wiring — executed step text under `bash -e`,
the step sequencing, and the aggregator. `qualify_*.py` (~2 minutes): real virtual
environments from synthetic wheels and the standard library's bundled pip, real
candidates, and the consumer's refusals. They are named apart so the Django runner's
`test*.py` discovery does not run them a second time inside the full suite.

The preflight has one of each. `tests_preflight.py` (fast) covers:

- request resolution;
- the certification matrix: each required job failing or missing, a partial re-run
  mixing attempts, another workflow, event, repository, commit or run, a commit not on
  main, an incomplete listing, a missing or expired or contradictory artifact;
- zip admission;
- the deadline arithmetic;
- the runner-authority and `pip.conf` isolation of the scanner's environment;
- the workflow contract: triggers, permissions, the token boundary, commit pins, and the
  committed step text executed for each exit status.

`qualify_preflight.py` (~90 seconds) builds real candidates through the real producer and
consumer and drives `assess` and `verify` end to end. It includes:

- digest and identity substitution;
- a tampered wheel and an unbound inventory;
- a foreign or failed reconstruction;
- a new advisory against an unchanged candidate;
- scanner failures reported as incomplete;
- every receiving refusal (time, raw output, inventory, scanner, policy, evaluator,
  expiry, scope);
- a candidate whose own policy and `release/preflight.py` try to decide for it;
- the real-pip `pip.conf` proof;
- the real CLI over a stub `gh`.

Advisory answers are synthetic there. The approvals it uses are marked fixtures, never
policy.

The installation has two fast files. `tests_host.py`: the profile (every unknown named;
`false` versus `null`), the admission and its margin, the runtime files and the identity a
launcher derives, the include, the migration decisions, the host lock and journal, and the
switch/restore/resume sequence through transport doubles. `tests_staged.py`: no second
active writer, the staged workflow's triggers, permissions and token boundary, the host
script executed with substituted placeholders, the ordering guard, the marker reader, the
committed profile's refusal and its attestation, and the discovery collector's read-only
contract. `misc_app/tests_release_identity.py` covers the two routes.

## What remains

- **The B3 cutover** (`release/CUTOVER.md`): owner-approved discovery on the real host, a
  reviewed verified profile, the identities, directories, bucket and grants, the
  configuration copy, the includes, and ONE reviewed change that activates the staged
  workflow and deletes `deploy-uat.yml`. Until then `deploy-uat.yml` is the live path, and
  nothing here protects it.
- A served release identity the frontend's gate can read. The routes exist (they answer
  `unavailable` under the legacy install); the frontend keeps refusing
  `peers.backend_serving_unverified` until a B3-installed release serves, correctly.
- Dinify-Admin's adoption of the shared host lock (staged in `HOST_LOCK_CONTRACT.md`).
- The B2.1 CI scanner install's configuration exposure above (the preflight's is closed).
