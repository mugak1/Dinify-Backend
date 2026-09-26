# dependency_audit

A required dependency audit (D08 B2.1): what was inspected, what the advisory data says
about it, and one policy decision — enforced inside every `suite` leg of `ci.yml`, and
therefore inside the required `test` aggregator. It is the Python port of
`dependency-audit/` in Dinify-Frontend and Dinify-Admin: `conformance.json` is
byte-identical in all three repositories, the JavaScript and Python evaluators are both
tested against every case, and each suite pins the file's digest.

**A clean application test suite is not a dependency audit, and a scheduled audit that no
required check consumes is not a gate.** This package puts the audit inside the required
check.

## What is inspected

Not `requirements.txt` resolved afresh. `pip-audit -r requirements.txt` resolves
dependencies on its own — it does NOT ignore transitives — but an independent resolution
is not the inventory the suite ran against. What is audited is the package inventory of
the **disposable Python 3.12.3 validation environment itself**, read with that
interpreter's own `pip inspect` right after it is installed: the pinned requirements,
their transitives (`cffi`, `pycparser`) and `pip` — 27 packages on main. Since D08 B2.5
that environment is the CERTIFIED one (`release/README.md`): built offline from exactly
the files `release/python-lock.json` names, the pinned pip 26.2.1 included, where it used
to be `pip install --upgrade pip` then `pip install -r requirements.txt` resolving the
transitives and pip at run time. Each carries a digest of its installed `RECORD`,
so a package that changes in place changes the inventory. That inventory is written out as
exact `name==version` pins and scanned with `--no-deps --disable-pip --strict`: pip-audit
resolves nothing, and a package it cannot audit is a failure, not a skip.

The snapshot is **refused** if a declared requirement is missing or at the wrong pin, a
package was installed from a direct URL or path (no index advisory can describe it), the
inventory is empty or unreadable, or the interpreter is not the policy's `target.python`
(3.12.3 — held equal to the `ci.yml` matrix pin and the `audit.yml` pin by a test). A 3.11
environment is not the one CI validates, and this does not pretend otherwise.

**The scanner has its own environment and is audited as its own graph.** A fresh venv is
created from the target interpreter and `scanner-requirements.txt` is installed into it
with `--require-hashes --no-deps --only-binary=:all: --isolated` — 29 packages, every one
hash-pinned, resolved no later than 2026-09-20. The target inventory is compared before and
after, so installing the scanner cannot silently move a target package. The scanner's own
inventory must equal the pinned set exactly, and is then scanned like any other tooling:
the venv's bundled `pip 24.0` would otherwise carry twelve advisory entries, which is why
`pip 26.2.1` is in the pinned set.

`pip`, `setuptools` and `wheel` are **tooling**; every other installed package is
**runtime**, the stricter scope.

## The policy

The same table as the npm repositories:

| finding | decision |
|---|---|
| critical or high, any scope | **blocking** |
| any severity on a runtime package | **blocking** |
| moderate / low / info on tooling | visible, **triage required** |
| anything the policy cannot evaluate | **incomplete** |

**pip-audit reports no severity**, so every Python finding is severity `unknown`: on a
runtime package it blocks; on `pip` it cannot be evaluated and the audit is incomplete.
That is the conservative reading the policy requires, not an approximation of one.

Outcomes and exit statuses: `within_policy` 0, `exceptions_only` 0, `blocking` 1,
`incomplete` 2. **An incomplete audit fails the required check.** Measured: when PyPI is
unreachable pip-audit exits **1 with empty stdout** — the same status it uses for
"vulnerabilities found" — so the status is only ever accepted when the JSON body agrees
with it, and every package in the inventory must appear in the report at its version.

pip and pip-audit configuration is never inherited: `PIP_*` (including `PIP_AUDIT_*`),
`PYTHON*`, `VIRTUAL_ENV` and `CONDA_PREFIX` are removed from the scanner's environment, the
index is named explicitly, and each scan gets a fresh cache directory so no earlier
advisory answer is reused.

## Exceptions and triage records

`policy.json → records` is empty, and **nothing in this change approves anything.** The
record schema, its refusals (expired, beyond 90 days, broadened, mismatched, stale,
duplicated, unknown fields, no linked review, an alias the scanner does not report) and its
matching rules are those of the npm repositories, pinned by the shared vectors. Which
advisories are the same is pip-audit's statement (its `id` and `aliases`), never the
record's: a record's aliases must all be corroborated by the finding it covers. A Python node path is
`application:site-packages/<name>` or `scanner:site-packages/<name>`.

## How it runs

```
python -B -m release acquire …; python -B -m release install …   # CI: the certified environment
python -m dependency_audit snapshot      # offline — this interpreter's inventory
…                                        # every existing gate, plus the offline matrix:
python -m unittest discover -t . -s dependency_audit -p "tests_*.py"
python -m dependency_audit self-test && python -m dependency_audit audit   # NETWORK
```

The audit writes `evidence/`: `snapshot.json`, `collection.json` (bindings, argv, exit
statuses, per-package inventory and digests), `result.json`, each graph's exact
`*.inventory-requirements.txt`, and each scan's complete raw stdout and stderr. CI uploads
it as `dependency-audit-<python>-<run>-<attempt>` whether the leg passed or not.
`python -m dependency_audit evaluate` re-decides retained evidence offline and refuses
evidence for another revision, inventory or environment, or raw output that is not the
recorded bytes. The Django test runner also discovers the `tests_*.py` here, so they run a
second time inside the full suite.

## The state at delivery

`main` (a6b25a6) audits **within policy, no advisories reported**, for both the 27-package
application inventory and the 29-package scanner venv. No Backend dependency changes.

## What this does not cover

Stated so none of it is inferred:

- **The live UAT venv is not observed.** `deploy-uat.yml` requires a successful Backend CI
  run on main — which now includes this audit — and then runs `pip install -r
  requirements.txt` into the serving venv on the box. That install resolves the unpinned
  transitives and `pip` independently, at deploy time. This audit describes the CI
  inventory; it is not an observation of what is installed on the host. D08 B2.5 added the
  lock and a certified candidate (`release/`), but the deploy does not consume either yet.
- **The scanner's own install honours the runner's global pip configuration.** It runs
  `pip install --isolated` with every `PIP_*` variable scrubbed, and `--isolated` ignores
  environment variables and the user's file but still reads the global `/etc/pip.conf`
  and a site `pip.conf` (measured in B2.5 — pip's only switch that loads no file at all is
  `PIP_CONFIG_FILE=/dev/null`, which the scrub removes). `--require-hashes
  --only-binary=:all:` still bounds what can be installed, so a configured source can only
  supply the SAME hashed files; the certified environment's install sets the switch and
  this one does not. Recorded, not changed: it is a scanner-policy change, outside B2.5.
  The same bootstrap run by the B2.6 preflight (`release/preflight.py`) DOES set the switch
  and withholds the runner's tokens and step-output files. A real-pip test shows a planted
  `pip.conf` steering this CI install and not the preflight's. So the exposure is closed
  on that path and remains on this one.
- **A fresh audit of what would be promoted is not a gate on the deploy.** D08 B2.6 adds a
  non-deploying preflight (`release/README.md` → "The preflight"). For one retained
  candidate, it asks this evaluator's question again, now, over the candidate's retained
  inventory (`--no-deps --disable-pip --strict`, a fresh cache). It decides under the
  trusted policy with a 24-hour window, and a separate receiving check reproduces the
  decision. `deploy-uat.yml` consumes none of it: a manual `workflow_dispatch` redeploy
  can still run long after the CI run it cites, and resolves `requirements.txt` on the
  box. Connecting the two is B3.
- **Not audited here:** the GitHub Actions used by the workflows, the runner image's
  tooling, and anything on the host.
- **Branch protection is not changed.** "The audit is wired into `test`" and "GitHub
  settings prevent bypassing `test`" are separate facts; this change establishes only the
  first.
- The self-tests/coverage work on the pre-existing money-field, tenant and
  ambient-authority scanners is a separate B2 item; those gates are untouched here.
