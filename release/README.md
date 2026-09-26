# Backend release candidate — D08 B2.5, and its preflight — D08 B2.6

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
hints that must agree. The result zip is admitted by that listing's digest, and the
receiving side then refuses unless all of the following hold:

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

## What remains

- **B3 — promotion.** A deploy that installs **this** candidate's environment rather than
  resolving `requirements.txt` on the box, and that repeats the preflight's receiving
  check at its own boundary before it does. Until then `deploy-uat.yml` is the live path,
  and nothing here protects it.
- A served release identity for the backend (B3). Until then the frontend's gate refuses
  `peers.backend_serving_unverified`, correctly.
- The B2.1 CI scanner install's configuration exposure above (the preflight's is closed).
