# Backend B3 cutover — the plan, and every step that needs the owner

**Nothing in this file has been done.** It is the specific, ordered plan for moving the UAT
Backend from the legacy in-place deploy (`.github/workflows/deploy-uat.yml`: `git checkout` in
`/home/ubuntu/dinify_backend_handover_uat`, `pip install -r requirements.txt` into a mutable
venv, `migrate`, `systemctl restart apache2`) to immutable releases installed by
`release/staged/host-run.sh`. Each step marked **OWNER** needs an explicit approval in its own
words before it is started; the implementation's merge is not that approval. No step here was
run against the real host, its database, its Apache or its secrets.

What the implementation has shown, and where: `release/README.md` → "The installation" (the
disposable rehearsal, real and modelled evidence kept apart). What it has NOT shown: anything
about the real host. The profile in `release/profiles/uat-backend.json` is `unverified`, and
`host deploy` refuses it naming every unknown field.

## 0. Preconditions (no host access)

- [ ] This change merged. The legacy deploy keeps running on every merge; the new identity
      routes (`/uat/api/v1/release/`, `/api/admin/v1/release/`) go live through it and answer
      `unavailable / not_started_by_release_launcher` — true, harmless, and the signal the
      staged ordering guard reads as "legacy".
- [ ] A backend change that introduces a migration has a reviewed entry in
      `release/migration-decisions.json` (exact file digest, exact operation types, `expand`),
      or the first B3 deploy of it will stop with `migration_unreviewed` — by design.

## 1. Read-only discovery — OWNER approves running it

- [ ] Run `release/staged/discover_host.py` on the instance as root through SSM
      (`python3 -I -B`), capture stdout and the two sha256 lines on stderr. It writes, sends,
      restarts and changes nothing, and never reads a secret value (see its docstring).
- [ ] Review the report in a PR: it becomes `release/profiles/uat-backend.json` with
      `status: verified` and `observation` naming the collector sha256, the report sha256, the
      collection time and that PR. Every null is filled FROM THE REPORT or by an explicit
      decision recorded in the PR (below), never from a hint.

Decisions that PR records, each explicitly:
- the preparation identity (proposal: a new locked, no-login system account `dinify-prep`,
  not in `www-data`) and the migration identity (proposal: `dinify-migrate`, in the group that
  can read the configuration, not the preparer); the runtime stays `www-data`;
- whether the deterministic test OTP (`ENV=dev`) is still wanted on UAT, with a reason ≥ 20
  characters if it is (`otp.deterministicTestOtpAllowed`); the config gate refuses `ENV=dev`
  otherwise;
- the daemon group names, processes/threads (from what serves today), mounts, whether this
  Apache serves each plane's static files and the media (`false` when it does not), the probe
  bases (loopback connect addresses);
- where media lives now and stays: `media.root` is OUTSIDE every release and is never copied.

## 2. Identities and directories — OWNER approves (host mutation)

- [ ] Create the preparation and migration accounts (no shell, no password, no sudo).
- [ ] Create root-owned `0755` `/srv/dinify-backend/releases`, root-owned `0700`
      `/srv/dinify-backend/state`, root-owned `0755` `/var/lib/dinify-host`, root-owned `0755`
      `/etc/apache2/dinify-backend`, root-owned `0711` `/opt/dinify-backend-release` with
      `incoming/` `0700` and `trusted/` `0711`. The two `0711`s are not a loosening: the
      preparation, migration and runtime identities import the trusted verifier and run from
      inside it, so they must be able to pass through (never list or write); the transferred
      files in `incoming/` stay private. `host-run.sh` and `host deploy` both refuse any other
      mode by name.
- [ ] Confirm no `.env` or `settings.ini` sits at or above `/srv/dinify-backend/releases`
      (`host_problems` refuses one: python-decouple searches upward from each release).

## 3. AWS — OWNER (account settings)

- [ ] An S3 bucket (or prefix) for backend transfers, private, with a lifecycle rule; set the
      repository variable `BACKEND_ARTIFACT_BUCKET`.
- [ ] Grant the existing deploy role `s3:PutObject` on that prefix and the instance profile
      `s3:GetObject` on it. No new role, no new trust, no stored credential.
- [ ] A GitHub environment `uat` for the deploy job (reviewers optional).

## 4. Configuration moves out of the tree — OWNER approves (secrets; copy, not rotate)

- [ ] Copy (do not rotate, do not print) the legacy `.env` into
      `/etc/dinify-backend/customer.env` and `/etc/dinify-backend/admin.env`, `root:www-data`
      `0640`, keys unchanged. The legacy `.env` stays where it is: the legacy deploy still reads it.
- [ ] `host validate-profile` on the host from a trusted checkout: must print "structurally valid (live, verified)".

## 5. The includes — OWNER approves (Apache change, reversible)

- [ ] Move the CURRENT WSGI directives for each plane, byte-for-byte as they are, out of the
      vhosts into `/etc/apache2/dinify-backend/dinify-backend-customer.conf` and `-admin.conf`,
      and `Include` them from the vhosts. `apachectl configtest`; graceful reload; the portal and
      the Admin must behave exactly as before (they run the same legacy processes).
- [ ] `host adopt-legacy --operation cutover-adopt`: records those files, never overwritten,
      as the target of a deliberate `host recover-legacy`.

## 6. The first B3 transition — OWNER approves (production switch)

In ONE reviewed change: move `release/staged/deploy-backend.yml` into `.github/workflows/`
AND delete `.github/workflows/deploy-uat.yml` (`release/tests_staged.py` fails the build if
both exist). Then:

- [ ] Merge → Backend CI → Candidate Preflight → the B3 deploy. Or dispatch it for the exact
      commit and preflight run.
- [ ] Read the job: `B3-ADMITTED`, `B3-PREPARED`, `B3-OUTCOME: verified`, `B3-SERVING` for both
      planes, and the public identity step. Both routes report the release id.

Expected cost of a switch, measured on the rehearsal host: a GRACEFUL reload, not a restart.
Apache reclaims the previous mod_wsgi daemons about **3 seconds** after the signal whatever
`shutdown-timeout` says, so a request still running then is cut off (500). The legacy deploy's
`systemctl restart apache2` cuts every in-flight request at once and briefly stops Apache.

## 7. Rollback and recovery — what exists after the cutover

- **Certified rollback** — dispatch the B3 deploy with `mode: rollback` for an older certified
  commit whose candidate and a fresh preflight are still retained. The host reuses the installed
  release after re-verifying every byte; migrations applied since must be reviewed `expand`
  (they appear to the older release as `appliedUnknown`), or it refuses.
- **Restoration** — automatic, inside a failed transition: the previous include files are put
  back and re-verified. A red run that says `restored` means nothing new serves.
- **Legacy recovery** — `host recover-legacy --operation <id>` puts back the adopted legacy
  directives. The legacy tree must still be there; it is reported as legacy (the processes are
  shown healthy; which code they loaded cannot be established).
- **An open operation** — `host status`, then `host resume --operation <id>`: it observes what
  serves, verifies it, and restores the kept previous files if a switch never verified.
  An operation that began applying migrations and never recorded that they completed stays
  OPEN after `resume` (`schema_state_unknown`) even when what serves verifies: re-reading the
  migration plan cannot describe a non-atomic migration that stopped half-way. **OWNER**:
  inspect the database, decide what state it is in (and any repair, which this path never
  performs), then `host resume --operation <id> --schema-established "<what was found>"`; the
  statement is recorded in the journal verbatim and is the only thing that closes it.

Nothing prunes releases. Retention is what keeps rollback cheap; a retention policy is a later,
separate decision.

## 8. After the cutover — OWNER

- [ ] Dinify-Admin adopts the shared host lock (`release/HOST_LOCK_CONTRACT.md`, staged patch),
      in its own reviewed change, before the two deploy paths can overlap.
- [ ] Decide when the legacy tree and `.env` in `/home/ubuntu` are retired (they are the
      legacy-recovery target until then).
