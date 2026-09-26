# Backend release candidate — D08 B2.5

**One retained candidate whose exact source and complete Python dependency files are the
ones its identified CI run validated and audited, and an independent proof that those
retained inputs alone rebuild the environment.** This directory is the producer, the
consumer and the rules both apply.

It does **not** deploy anything. `deploy-uat.yml` is untouched: it still checks out the
commit on the box and runs `pip install -q -r requirements.txt` into the shared serving
venv, so **the live host has not acquired the guarantee this candidate carries** (a test
pins that statement, so it cannot quietly become false). Connecting promotion to the
candidate is later B2 work.

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

## Exit codes

`python -B -m release …`: **0** accepted · **1** refused (every problem is printed with a
stable code) · **64** usage. There is no "incomplete but OK".

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
fails if it is removed. The B2.1 scanner install has the same exposure to the global file
(`--require-hashes` still bounds it to the same hashed files); that is recorded in
`dependency_audit/README.md`, not changed here.

## Tests

`tests_*.py` (fast, run by `unittest discover -p "tests_*.py"`): the lock rules, the
source and archive rules, and the workflow wiring — executed step text under `bash -e`,
the step sequencing, and the aggregator. `qualify_*.py` (~2 minutes): real virtual
environments from synthetic wheels and the standard library's bundled pip, real
candidates, and the consumer's refusals. They are named apart so the Django runner's
`test*.py` discovery does not run them a second time inside the full suite.

## What remains (later B2)

- Promotion: a deploy that installs **this** candidate's environment rather than
  resolving `requirements.txt` on the box, with a fresh audit bound to what it promotes.
- A served release identity for the backend (B3); until then the frontend's gate
  refuses `peers.backend_serving_unverified`, correctly.
- The scanner-install configuration exposure above.
