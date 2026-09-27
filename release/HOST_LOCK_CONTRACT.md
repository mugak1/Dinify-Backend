# The shared host lock and journal — D08 B3

The UAT instance (`i-0eeb7c0c3a36d3667`) serves THREE things through ONE Apache: the Backend
customer plane, the Backend admin plane, and the Admin SPA (`admin.dinifyapp.com`, a symlink
switch under `/var/www`). Two repositories change that host: this one (`release/transition.py`,
through `release/staged/host-run.sh`) and Dinify-Admin (its `deploy.yml` host procedure).
GitHub's `concurrency` groups serialize each repository's own runs and nothing across them. The
host serializes itself, with this contract.

**Status: this repository IMPLEMENTS it (`release/transition.py::HostLock`, exercised on a
disposable host). Dinify-Admin does NOT yet — its integration is STAGED below and needs its own
reviewed change in that repository. Until it lands, a Backend transition and an Admin promotion
are not mutually excluded on the host, exactly as today.**

## The lock

- **One file**, `profile.lockPath` (`/var/lib/dinify-host/host.lock` in the committed UAT
  profile; the directory is root-owned `0755`, created by the cutover and never by a deploy).
- **`flock(2)` exclusive, on an open file descriptor, held for the whole mutation** — from the
  first byte written anywhere a request can reach (a release directory's promotion, an include
  file, a symlink, an Apache reload) until the post-change verification has finished or the
  restoration has. A reader (status, discovery) takes no lock.
- **Bounded wait, never a steal.** A writer waits a bounded time (Backend: 300 s) and then
  refuses with `host_locked`, naming the recorded holder. Nobody breaks a lock: a stale lock is
  impossible by construction, because `flock` is released by the kernel when its holder exits.
- **The holder is recorded beside the lock** in `<lockPath>.holder` (`{"holder", "since", "pid"}`, mode `0600`),
  written after acquisition and read by a waiter only to explain the wait. It is advisory text,
  never the lock.
- **Lock order.** The host lock is taken FIRST, before any repository-specific lock (the
  Backend's per-release construction lock, `releases/.locks/<release>.lock`). Nothing takes the
  host lock while holding another.

## The journal (Backend)

`<stateDir>/operations/<operation>.jsonl`, one JSON object per line, appended and `fsync`ed,
stages in this vocabulary and no other: `locked · unchanged · gated · migrated · switching ·
switched · verified · verification-failed · restored · restoration-failed · refused · resumed`.
`<stateDir>/active.json` names the ONE open operation; a new transition refuses while it exists
(`previous_operation_unresolved`), and `host resume --operation <id>` settles it by observing what serves. The
previous include files are kept beside the journal (`<stateDir>/operations/<id>/previous-*.conf`)
before anything is replaced. The journal is Backend-only; Admin keeps its own records.

## Staged Admin integration (NOT applied — for a reviewed Dinify-Admin change)

In Dinify-Admin's host procedure (the script `deploy.yml` sends through SSM), around the block
that stages, verifies and promotes a release and checks it through Apache:

```bash
LOCK=/var/lib/dinify-host/host.lock          # the same path as the Backend profile's lockPath
[ -d "$(dirname "$LOCK")" ] || fail "the shared host lock directory is missing (Backend CUTOVER step 4)"
exec 9>>"$LOCK"
flock -w 300 9 || fail "host_locked: another host transition holds $LOCK: $(cat "$LOCK.holder" 2>/dev/null)"
( umask 077; printf '{"holder":"admin:%s","since":"%s","pid":%d}\n' "$TARGET_SHA" "$(date -u +%FT%TZ)" "$$" > "$LOCK.holder" )
# ... existing staging, promotion and post-promotion checks, unchanged ...
# fd 9 closes when the script exits; the kernel releases the lock.
```

Two properties the Admin change must keep: the lock is held through its own post-switch
verification and any restoration (a Backend graceful reload in the middle of Admin's public
checks would make them flap), and a lock wait that times out is a refusal before anything
moved, never a partial promotion.
