"""STAGED — reads the box's own attestation out of the SSM output (D08 B3).

The green run is decided by what the HOST printed, never by what the workflow sent:

    B3-ADMITTED: <commit> <candidate digest> preflight <digest> until <deadline>
    B3-PREPARED: <release id> (installed|reused)
    B3-OUTCOME:  verified | unchanged | refused | restored | verification-failed | restoration-failed
    B3-SERVING:  <plane> <release id>          one per plane on a verified/unchanged outcome
    B3-EXIT:     <status>

Each attestation must appear exactly once (SSM keeps only the FIRST 24,000 characters, so
host-run.sh prints them before anything else). Exit 0 verified/unchanged, 1 refused, 3
restored (red: nothing new serves, the previous release was verified serving again), 4
restoration failed (DEGRADED: the host needs `host status` / `host resume` by an operator).
"""

import argparse
import re
import sys

EXIT = {"verified": 0, "unchanged": 0, "refused": 1, "restored": 3, "verification-failed": 3, "restoration-failed": 4}
_RID = re.compile(r"^[0-9a-f]{40}-[0-9a-f]{16}$")


def read(text, commit, ssm_status):
    """Returns (exit, release_id, message)."""
    lines = [line.strip() for line in text.splitlines()]

    def one(prefix):
        found = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
        return found[0] if len(found) == 1 else None, len(found)

    outcome, n = one("B3-OUTCOME:")
    if n != 1 or outcome not in EXIT:
        return 4, None, "the box printed %d outcome attestations; the host state is unknown (run host status)" % n
    if outcome in ("verified", "unchanged"):
        admitted, _ = one("B3-ADMITTED:")
        prepared, _ = one("B3-PREPARED:")
        if ssm_status != "Success":
            return 4, None, "the box attested %s but SSM reported %s" % (outcome, ssm_status)
        if not admitted or admitted.split()[0] != commit:
            return 4, None, "the box did not attest admitting %s" % commit
        rid = (prepared or "").split()[0] if prepared else ""
        serving = [line.split()[1:] for line in lines if line.startswith("B3-SERVING:")]
        if not _RID.match(rid) or not rid.startswith(commit + "-") or sorted(serving) != [["admin", rid], ["customer", rid]]:
            return 4, None, "the box's serving attestation does not name the prepared release on both planes"
        return 0, rid, "the box attests %s serving on both planes (%s)" % (rid, outcome)
    if outcome == "refused":
        return 1, None, "the host refused before switching; the previous release still serves"
    if outcome in ("restored", "verification-failed"):
        return 3, None, "the new release did not verify; the previous one was restored and verified serving"
    return 4, None, "DEGRADED: the restoration did not verify; an operator must run host status / resume"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--box", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    with open(args.box, encoding="utf-8", errors="replace") as fh:
        code, rid, message = read(fh.read(), args.commit, args.status)
    print(("" if code == 0 else "::error::") + message, file=sys.stderr if code else sys.stdout)
    if rid:
        with open(args.output, "a", encoding="utf-8") as fh:
            fh.write("release_id=%s\n" % rid)
    return code


if __name__ == "__main__":
    sys.exit(main())
