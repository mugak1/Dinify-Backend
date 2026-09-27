"""STAGED — the deploy workflow's two public reads of what the host SERVES (D08 B3).

Standard library only; run from the trusted verifier checkout, never from a candidate.

    ordering.py --mode deploy|rollback --target SHA --output FILE
        The ordering guard, BEFORE any credential. Reads both planes' loaded-process identity
        (``/api/v1/release/`` and ``/admin/v1/release/`` under each plane's public base, from
        the committed host profile) and decides from what the processes REPORT, never from
        what a workflow believes it last deployed:

          both planes legacy (``unavailable``)      proceed (the first B3 deploy)
          the planes disagree, or either is a
          ``mismatch`` or unreadable                 refuse: the host needs `host status` /
                                                     `host resume`, not another switch
          served == target                           proceed (the host answers ``unchanged``)
          target descends from served                proceed
          target is an ancestor of served            automatic/deploy: a clean SKIP;
                                                     rollback: proceed (the deliberate move)
          diverged                                   refuse

    ordering.py --expect-serving RELEASE_ID
        After the box attested: both planes, read over the public internet, must report the
        verified identity of exactly that release, answered ``no-store``. One vantage point at
        one moment — it corroborates the box's own attestation, it does not replace it.

Exit 0 decided (proceed or skip, written to --output), 1 refused.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(os.path.dirname(HERE), "profiles", "uat-backend.json")
PATHS = {"customer": "/api/v1/release/", "admin": "/admin/v1/release/"}
SCHEMA = "dinify.backend.runtime-identity/1"
REPOSITORY = "mugak1/Dinify-Backend"


def _get(url, headers=None, timeout=15):
    request = urllib.request.Request(url, headers=dict({"User-Agent": "dinify-b3-ordering"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(1 << 16)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), b""
    except (urllib.error.URLError, OSError) as error:
        return None, {}, type(error).__name__.encode()


def bases(profile_path=PROFILE):
    with open(profile_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    found = {plane: ((doc.get("planes") or {}).get(plane) or {}).get("probe", {}).get("base") for plane in PATHS}
    missing = [plane for plane, base in found.items() if not base]
    return found, missing


def read_identity(base, plane, fetch=_get):
    """Returns (state, doc, problem): state is verified / legacy / None (unusable)."""
    status, headers, body = fetch(base.rstrip("/") + PATHS[plane])
    if status != 200:
        return None, None, "%s identity route answered %s" % (plane, status)
    cache = {k.lower(): v for k, v in headers.items()}.get("cache-control", "")
    if "no-store" not in cache:
        return None, None, "%s identity was not answered no-store" % plane
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError:
        return None, None, "%s identity is not JSON" % plane
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("plane") != plane:
        return None, None, "%s identity is not a %s document for this plane" % (plane, SCHEMA)
    if doc.get("state") == "unavailable" and doc.get("reason") == "not_started_by_release_launcher":
        return "legacy", doc, None
    if doc.get("state") == "verified" and isinstance(doc.get("release"), dict):
        return "verified", doc, None
    return None, doc, "%s reports %s (%s)" % (plane, doc.get("state"), doc.get("reason"))


def compare(base_sha, head_sha, token, fetch=_get):
    """GitHub's own answer: ahead / behind / identical / diverged, or None."""
    status, _, body = fetch("https://api.github.com/repos/%s/compare/%s...%s" % (REPOSITORY, base_sha, head_sha),
                            {"Authorization": "Bearer %s" % token, "Accept": "application/vnd.github+json"} if token else None)
    if status != 200:
        return None
    try:
        return json.loads(body.decode("utf-8")).get("status")
    except ValueError:
        return None


def decide(mode, target, served, relation):
    """The whole ordering rule, pure. ``served`` is {plane: (state, doc)}. Returns
    (proceed, reason) or raises ValueError(reason) for a refusal."""
    states = {plane: state for plane, (state, _) in served.items()}
    if any(state is None for state in states.values()):
        raise ValueError("a plane's identity is unusable")
    if set(states.values()) == {"legacy"}:
        return True, "both planes are the legacy installation: this is the first B3 transition"
    if "legacy" in states.values():
        raise ValueError("one plane is legacy and the other is not: settle the host (host status / resume) before any switch")
    ids = {doc["release"]["id"] for _, doc in served.values()}
    if len(ids) != 1:
        raise ValueError("the planes serve different releases (%s): settle the host before any switch" % ", ".join(sorted(ids)))
    commit = next(iter(served.values()))[1]["release"]["commit"]
    if commit == target:
        return True, "the target is served; the host answers unchanged or installs a differently certified candidate"
    if relation in ("ahead",):
        return True, "the target descends from the served commit %s" % commit
    if relation == "behind":
        if mode == "rollback":
            return True, "a deliberate rollback from %s" % commit
        return False, "SKIP: the target is behind the served commit %s; a newer release already serves" % commit
    raise ValueError("the target and the served commit %s have diverged (or GitHub could not say): %s" % (commit, relation))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("deploy", "rollback"))
    parser.add_argument("--target")
    parser.add_argument("--output")
    parser.add_argument("--expect-serving")
    parser.add_argument("--profile", default=PROFILE)
    args = parser.parse_args(argv)
    found, missing = bases(args.profile)
    if missing:
        print("::error::the host profile has no public probe base for %s (the profile is unverified)" % ", ".join(missing), file=sys.stderr)
        return 1
    if args.expect_serving:
        problems = []
        for attempt in range(5):
            problems = []
            for plane, base in found.items():
                state, doc, problem = read_identity(base, plane)
                if problem or state != "verified" or doc["release"]["id"] != args.expect_serving:
                    problems.append(problem or "%s serves %s" % (plane, (doc or {}).get("release")))
            if not problems:
                print("PUBLIC-IDENTITY: both planes report %s" % args.expect_serving)
                return 0
            time.sleep(10)
        print("::error::%s" % "; ".join(problems), file=sys.stderr)
        return 1
    served = {}
    for plane, base in found.items():
        state, doc, problem = read_identity(base, plane)
        if problem:
            print("::error::%s" % problem, file=sys.stderr)
        served[plane] = (state, doc)
    relation = None
    try:
        states = {s for s, _ in served.values()}
        if states == {"verified"}:
            commit = next(iter(served.values()))[1]["release"]["commit"]
            relation = "identical" if commit == args.target else compare(commit, args.target, os.environ.get("GH_TOKEN"))
        proceed, reason = decide(args.mode, args.target, served, relation)
    except ValueError as error:
        print("::error::ORDERING REFUSED: %s" % error, file=sys.stderr)
        return 1
    print("ORDERING: %s" % reason)
    with open(args.output, "a", encoding="utf-8") as fh:
        fh.write("proceed=%s\n" % ("true" if proceed else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
