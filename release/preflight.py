"""THE PREFLIGHT (D08 B2.6) — given ONE retained Backend candidate, establish from OUTSIDE it
that its CI run really certified it, ask the advisory question again NOW over exactly the
inventory it retains, and leave a small, time-limited result that a later installer can
check for itself. It deploys nothing, observes no host and mutates nothing.

    facts     READ TOKEN, no scanner. The run, the attempt, that attempt's jobs, the run's
              artifact listing, the commit and its tree, main, and the trusted trees, as the
              GitHub API states them; then the candidate and reconstruction ZIPS, by id,
              written as bytes and not opened.
    assess    NO TOKEN. Select again from those facts; admit each zip only if its sha256 is
              the listing's digest, then unpack it with member rules; verify the candidate
              with the B2.5 consumer (nothing in it executes); bind the reconstruction report
              to that exact record; then a FRESH advisory query over the retained inventory
              with the TRUSTED pinned scanner and decide under the TRUSTED policy.
    verify    NO TOKEN. The receiving side: select from its OWN facts, find the result in the
              evaluation run's listing, and refuse unless the result is bound to this
              candidate, this certification, this evaluator and this policy, its decision
              reproduces from its raw output, its times are ordered inside GitHub's own
              bounds, and it has not expired.

WHAT "FROM OUTSIDE THE ARTIFACT" MEANS. ``promotable: true`` in a record is eligibility
metadata; the artifact is uploaded by the ``suite`` leg BEFORE ``reconstruct`` and ``test``
run. So certification is read from the API: a completed, successful push to main by the
workflow whose path is ci.yml (not merely its display name), for the exact commit, on an
attempt whose OWN jobs — the suite leg, the reconstruction and the aggregate — all
succeeded. A partial re-run cannot mix one attempt's candidate with another attempt's
checks: any job not carried by the selected attempt is refused.

THE APPLICATION INVENTORY IS THE RETAINED ONE. The query is over the exact ``name==version``
set the candidate's certification snapshot records (bound to the record's audited
inventory digest), classified by the TRUSTED scope rule, scanned with ``--no-deps
--disable-pip --strict``: nothing is installed, resolved or built, and no candidate code,
script or hook runs. The environment certification installed is NOT re-observed — those
bytes are gone — and nothing here says it was. The scanner's own graph IS observed, as
installed now from the trusted hash-pinned requirements.

THE TRUSTED POLICY DECIDES. ``dependency_audit/policy.json`` of the verifier's own checkout,
identified by the sha256 of its bytes — never the policy inside the candidate, which only
reproduces what certification decided (the B2.5 consumer still verifies that historical
decision, unchanged). A certification-time exception that lapsed, or that the trusted
policy no longer carries, does not survive into the fresh decision.

FRESHNESS is 24 hours from the actual start of the evaluation (earlier than the first
query, so never generous), cut short by the lapse of any record the decision applied. The
receiving side recomputes the deadline and requires a margin before it. A result whose
evaluator or policy is no longer what main carries is not refreshed: a new evaluation is.

Exit status: 0 accepted · 1 refused or blocking · 2 the assessment could not be completed
· 64 usage. A green run says the evidence checks passed; ``deploymentAuthorized`` is false
in every result this module writes, and nothing here is production readiness.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

from . import candidate as cd
from . import consumer as cs
from . import lockfile as lf
from . import sourcetree as st

PREFLIGHT_SCHEMA = "dinify.backend.preflight/1"
PREFLIGHT_DOC = "preflight.json"
PREFLIGHT_WORKFLOW_PATH = ".github/workflows/preflight.yml"
PREFLIGHT_PREFIX = "backend-preflight"
RECONSTRUCTION_PREFIX = "backend-reconstruction"
RECONSTRUCTION_FILE = "reconstruction.json"
# The certifying run's own jobs, by the names GitHub gives them. `suite (3.12.3)` carries
# the matrix value; a test holds this equal to the committed ci.yml, so a re-pinned leg
# cannot silently stop being required here.
REQUIRED_JOBS = ("suite (3.12.3)", "reconstruct", "test")
TRUSTED_TREES = ("release", "dependency_audit")
ASSESSMENT_WINDOW_HOURS = 24          # D08: the same maximum window Frontend and Admin enforce
RECEIVING_MARGIN_MINUTES = 30         # Admin's privileged margin; B3 checks again at its own boundary
CLOCK_SKEW_SECONDS = 120              # only ever between the runner's clock and GitHub's, never inside one document
GRAPHS = ("application", "scanner")
PASSING = cd.ACCEPTED_AUDIT_OUTCOMES
API_TIMEOUT, ZIP_TIMEOUT = 120, 600
MAX_ZIP_MEMBERS, MAX_MEMBER_BYTES, MAX_UNPACKED_BYTES = 4096, 256 << 20, 1 << 30
SCOPE = {
    "kind": "non-deploying preflight",
    "deploymentAuthorized": False,
    "hostObserved": False,
    "hostMutated": False,
    "statement": "a passing preflight means its evidence checks passed at the time recorded; nothing was deployed, "
                 "no host was observed or changed, and it is not production readiness",
}
FRESHNESS = ("a new query: the pinned pip-audit, installed now from the trusted hash-pinned requirements, asked PyPI's "
             "vulnerability service about each exact name==version with a fresh, empty --cache-dir per graph that is "
             "removed afterwards; no answer cached by an earlier scan is available to it (any caching on PyPI's side is "
             "outside this client)")
RAW_FILES = tuple("%s.%s" % (g, s) for g in GRAPHS for s in ("inventory-requirements.txt", "scanner-stdout.txt", "scanner-stderr.txt"))
# What a scanner subprocess must never inherit: the runner's step-output files and any token.
RUNNER_AUTHORITY = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE", "GITHUB_TOKEN", "GH_TOKEN")

_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_ATTEMPT = re.compile(r"^[1-9][0-9]{0,3}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")

FACT_FILES = {
    "workflow": "ci-workflow.json", "run": "run.json", "jobs": "jobs.json", "artifacts": "artifacts.json",
    "commit": "commit.json", "main": "main.json", "compare": "compare.json", "evaluatorCommit": "evaluator-commit.json",
    "evaluatorTree": "evaluator-tree.json", "mainTree": "main-tree.json", "choice": "choice.json",
    "evaluationRun": "evaluation-run.json", "evaluationArtifacts": "evaluation-artifacts.json",
}
ZIPS = "zips"


def _problem(code, detail):
    return {"code": code, "detail": detail}


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms(text):
    from dependency_audit import core
    return core.parse_instant(text)


def _iso(ms):
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def candidate_name(run_id, attempt):
    return cd.artifact_name(True, run_id, attempt)


def reconstruction_name(run_id, attempt):
    return "%s-%s-%s" % (RECONSTRUCTION_PREFIX, run_id, attempt)


def preflight_name(run_id, attempt):
    return "%s-%s-%s" % (PREFLIGHT_PREFIX, run_id, attempt)


# --- the request ---------------------------------------------------------------------------

def resolve_request(env):
    """Which candidate is being asked about, from the event — validated, never interpolated.

    Automatic (``workflow_run``): the triggering run and attempt and its head commit, and
    nothing else. Manual (``workflow_dispatch``): an exact commit, and optionally the CI run
    and attempt; without a run, the only successful push-to-main run for that commit is used
    and more than one is refused as ambiguous. Returns ``(request, problems)``."""
    problems = []
    event, ref = env.get("PREFLIGHT_EVENT"), env.get("GITHUB_REF")
    if ref != cd.MAIN_REF:
        problems.append(_problem("request_invalid", "the preflight runs from %s only (it ran from %r)" % (cd.MAIN_REF, ref)))
    if event == "workflow_run":
        req = {"source": "automatic", "target": env.get("PREFLIGHT_EVENT_SHA", ""), "run": env.get("PREFLIGHT_EVENT_RUN", ""),
               "attempt": env.get("PREFLIGHT_EVENT_ATTEMPT", "")}
        if not _ID.match(req["run"]) or not _ATTEMPT.match(req["attempt"]):
            problems.append(_problem("request_invalid", "the workflow_run payload carries no usable run id and attempt"))
    elif event == "workflow_dispatch":
        req = {"source": "manual", "target": env.get("PREFLIGHT_INPUT_SHA", ""), "run": env.get("PREFLIGHT_INPUT_RUN", "") or None,
               "attempt": env.get("PREFLIGHT_INPUT_ATTEMPT", "") or None}
        if req["run"] is not None and not _ID.match(req["run"]):
            problems.append(_problem("request_invalid", "ci_run_id must be empty or a numeric run id"))
        if req["attempt"] is not None and (req["run"] is None or not _ATTEMPT.match(req["attempt"])):
            problems.append(_problem("request_invalid", "ci_run_attempt needs ci_run_id and must be a numeric attempt"))
    else:
        return None, problems + [_problem("request_invalid", "event %r does not request a preflight" % event)]
    if not _SHA.match(req["target"] or ""):
        problems.append(_problem("request_invalid", "the target must be a full 40-character lowercase commit id"))
    return (None if problems else req), problems


def choose_run(request, latest=None, listing=None):
    """``(run, attempt)`` for a request, or problems. ``latest`` is GET /actions/runs/{run};
    ``listing`` is the successful push-to-main runs of ci.yml for the target."""
    if request["run"] and request["attempt"]:
        return (request["run"], request["attempt"]), []
    if request["run"]:
        if not isinstance(latest, dict) or str(latest.get("id")) != request["run"] or not isinstance(latest.get("run_attempt"), int):
            return None, [_problem("certification_unavailable", "run %s could not be read" % request["run"])]
        return (request["run"], str(latest["run_attempt"])), []
    runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
    if not isinstance(runs, list) or listing.get("total_count") != len(runs):
        return None, [_problem("certification_listing_incomplete", "the runs listing for %s is unreadable or incomplete" % request["target"])]
    ids = sorted({(r.get("id"), r.get("run_attempt")) for r in runs if isinstance(r, dict) and r.get("head_sha") == request["target"]
                  and r.get("event") == "push" and r.get("head_branch") == "main" and r.get("conclusion") == "success"
                  and r.get("path") == cd.WORKFLOW_PATH}, key=str)
    if not ids:
        return None, [_problem("certification_not_found", "no successful push to main of %s certified %s" % (cd.WORKFLOW_PATH, request["target"]))]
    if len(ids) > 1:
        return None, [_problem("certification_ambiguous", "%d successful push-to-main runs certified %s (%s); name one with "
                               "ci_run_id — an arbitrary historical green run is never chosen" % (len(ids), request["target"], ", ".join(str(i[0]) for i in ids)))]
    return (str(ids[0][0]), str(ids[0][1])), []


# --- facts (GitHub API, read token) ---------------------------------------------------------

def _gh(gh, endpoint, destination, timeout=API_TIMEOUT, env=None):
    """``gh api <endpoint>`` into ``destination``; returns a problem or None. The body is
    written as bytes; nothing is interpreted here."""
    with open(destination, "wb") as out:
        try:
            proc = subprocess.run([gh, "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
                                   endpoint], stdout=out, stderr=subprocess.PIPE, timeout=timeout, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            proc, failure = None, type(error).__name__
    if proc is None or proc.returncode != 0:
        os.remove(destination)   # a partial answer is no answer: nothing half-written is left to be read later
        return _problem("facts_unavailable", "GET %s failed (%s): %s" % (endpoint, failure if proc is None else "status %s" % proc.returncode,
                                                                         "" if proc is None else proc.stderr.decode("utf-8", "replace")[-300:]))
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_fact(facts_dir, key):
    path = os.path.join(facts_dir, FACT_FILES[key])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as error:
        return None, _problem("facts_unreadable", "%s: %s" % (FACT_FILES[key], type(error).__name__))
    if not isinstance(doc, dict):
        return None, _problem("facts_unreadable", "%s is not an object" % FACT_FILES[key])
    return doc, None


def fetch_facts(out, request, revision, gh="gh", repository=cd.REPOSITORY, evaluation=None):
    """Read every fact the preflight needs and download the zips BY ID, unopened. Selection
    problems do not stop the fetch — the assessment reports them and a refused result is
    retained; a transport failure does. Returns ``(choice, problems)``."""
    os.makedirs(os.path.join(out, ZIPS), exist_ok=True)
    base = "/repos/%s" % repository
    path = lambda key: os.path.join(out, FACT_FILES[key])  # noqa: E731
    latest = listing = None
    if request["run"] and not request["attempt"]:
        err = _gh(gh, "%s/actions/runs/%s" % (base, request["run"]), os.path.join(out, "run-latest.json"))
        if err:
            return None, [err]
        latest = _load(os.path.join(out, "run-latest.json"))
    elif not request["run"]:
        err = _gh(gh, "%s/actions/workflows/ci.yml/runs?head_sha=%s&branch=main&event=push&status=success&per_page=100"
                  % (base, request["target"]), os.path.join(out, "runs.json"))
        if err:
            return None, [err]
        listing = _load(os.path.join(out, "runs.json"))
    chosen, problems = choose_run(request, latest, listing)
    if problems:
        return None, problems
    run_id, attempt = chosen
    choice = {"target": request["target"], "runId": run_id, "runAttempt": attempt, "source": request["source"], "revision": revision}
    with open(path("choice"), "w", encoding="utf-8") as fh:
        json.dump(choice, fh, indent=2, sort_keys=True)
    plan = [("workflow", "%s/actions/workflows/ci.yml" % base), ("run", "%s/actions/runs/%s/attempts/%s" % (base, run_id, attempt)),
            ("jobs", "%s/actions/runs/%s/attempts/%s/jobs?per_page=100" % (base, run_id, attempt)),
            ("artifacts", "%s/actions/runs/%s/artifacts?per_page=100" % (base, run_id)),
            ("commit", "%s/git/commits/%s" % (base, request["target"])), ("main", "%s/commits/main" % base),
            ("evaluatorCommit", "%s/git/commits/%s" % (base, revision))]
    for key, endpoint in plan:
        err = _gh(gh, endpoint, path(key))
        if err:
            return choice, [err]
    main, _ = read_fact(out, "main")
    evaluator, _ = read_fact(out, "evaluatorCommit")
    main_sha, main_tree = (main or {}).get("sha"), ((main or {}).get("commit") or {}).get("tree", {}).get("sha")
    eval_tree = ((evaluator or {}).get("tree") or {}).get("sha")
    if not all(_SHA.match(str(v)) for v in (main_sha, main_tree, eval_tree)):
        return choice, [_problem("facts_unreadable", "main or the evaluator revision has no readable commit and tree")]
    for key, endpoint in (("compare", "%s/compare/%s...%s?per_page=1" % (base, request["target"], main_sha)),
                          ("mainTree", "%s/git/trees/%s" % (base, main_tree)), ("evaluatorTree", "%s/git/trees/%s" % (base, eval_tree))):
        err = _gh(gh, endpoint, path(key))
        if err:
            return choice, [err]
    if evaluation:
        for key, endpoint in (("evaluationRun", "%s/actions/runs/%s/attempts/%s" % (base, evaluation["runId"], evaluation["runAttempt"])),
                              ("evaluationArtifacts", "%s/actions/runs/%s/artifacts?per_page=100" % (base, evaluation["runId"]))):
            err = _gh(gh, endpoint, path(key))
            if err:
                return choice, [err]
    selection, problems = select(out, choice)
    if problems:
        return choice, []   # reported, and retained, by the assessment
    downloads = [("candidate", selection["candidate"]["id"]), ("reconstruction", selection["reconstruction"]["id"])]
    if evaluation:
        found, problems = find_preflight(out, evaluation)
        if problems:
            return choice, []
        downloads.append(("preflight", found["id"]))
    for label, artifact_id in downloads:
        err = _gh(gh, "%s/actions/artifacts/%s/zip" % (base, artifact_id), os.path.join(out, ZIPS, label + ".zip"), timeout=ZIP_TIMEOUT)
        if err:
            return choice, [err]
    return choice, []


# --- selection (pure, over the facts) --------------------------------------------------------

def _listed_artifact(entry, run_id, target, problems, label):
    good = (isinstance(entry, dict) and isinstance(entry.get("id"), int) and entry["id"] > 0 and _DIGEST.match(str(entry.get("digest")))
            and isinstance(entry.get("size_in_bytes"), int))
    if not good:
        problems.append(_problem("certification_listing_incomplete", "%s carries no usable id, digest or size in the listing" % label))
        return None
    if entry.get("expired") is not False:
        problems.append(_problem("certification_expired", "%s (artifact %s) has expired or states no expiry; it cannot be assessed" % (label, entry["id"])))
    wr = entry.get("workflow_run") or {}
    if str(wr.get("id")) != str(run_id) or wr.get("head_sha") != target:
        problems.append(_problem("certification_wrong_run", "%s is not listed as run %s's for %s" % (label, run_id, target)))
    return {"id": str(entry["id"]), "name": entry.get("name"), "digest": entry["digest"], "size": entry["size_in_bytes"],
            "createdAt": entry.get("created_at"), "expiresAt": entry.get("expires_at")}


def select(facts_dir, choice, repository=cd.REPOSITORY):
    """The certification and its artifacts, from the API's own answers. Returns
    ``(selection, problems)``. Nothing is downloaded or opened here."""
    problems, docs = [], {}
    if not isinstance(choice, dict) or not _SHA.match(str(choice.get("target"))) or not _ID.match(str(choice.get("runId"))) \
            or not _ATTEMPT.match(str(choice.get("runAttempt"))):
        return None, [_problem("request_invalid", "no valid target, run and attempt were chosen")]
    target, run_id, attempt = choice["target"], choice["runId"], choice["runAttempt"]
    for key in ("workflow", "run", "jobs", "artifacts", "commit", "main", "compare"):
        docs[key], err = read_fact(facts_dir, key)
        if err:
            problems.append(err)
    if problems:
        return None, problems
    wf, run, jobs, arts = docs["workflow"], docs["run"], docs["jobs"], docs["artifacts"]

    def want(code, ok, detail):
        if not ok:
            problems.append(_problem(code, detail))

    want("certification_wrong_workflow", wf.get("path") == cd.WORKFLOW_PATH and isinstance(wf.get("id"), int)
         and run.get("workflow_id") == wf.get("id") and run.get("path") == cd.WORKFLOW_PATH,
         "run %s belongs to workflow %s (%s), not %s (%s)" % (run.get("id"), run.get("workflow_id"), run.get("path"), cd.WORKFLOW_PATH, wf.get("id")))
    want("certification_wrong_repository", (run.get("repository") or {}).get("full_name") == repository
         and (run.get("head_repository") or {}).get("full_name") == repository,
         "run %s is not a run of %s on its own branch" % (run.get("id"), repository))
    want("certification_wrong_run", str(run.get("id")) == run_id and str(run.get("run_attempt")) == attempt,
         "the run read is %s attempt %s, not %s attempt %s" % (run.get("id"), run.get("run_attempt"), run_id, attempt))
    want("certification_wrong_event", run.get("event") == "push" and run.get("head_branch") == "main",
         "run %s was a %s on %s; only a push to main certifies" % (run_id, run.get("event"), run.get("head_branch")))
    want("certification_wrong_commit", run.get("head_sha") == target, "run %s is for %s, the target is %s" % (run_id, run.get("head_sha"), target))
    want("certification_not_successful", run.get("status") == "completed" and run.get("conclusion") == "success",
         "run %s attempt %s is %s/%s" % (run_id, attempt, run.get("status"), run.get("conclusion")))

    job_list = jobs.get("jobs")
    if not isinstance(job_list, list) or jobs.get("total_count") != len(job_list):
        problems.append(_problem("certification_jobs_incomplete", "the job listing of run %s attempt %s is unreadable or incomplete" % (run_id, attempt)))
        job_list = []
    selected_jobs = []
    for job in job_list:
        if not isinstance(job, dict):
            problems.append(_problem("certification_jobs_incomplete", "a job entry is malformed"))
            continue
        if str(job.get("run_attempt")) != attempt or str(job.get("run_id")) != run_id or job.get("head_sha") != target:
            problems.append(_problem("certification_mixed_attempt", "job %r is carried from run %s attempt %s for %s, not produced by attempt %s: a "
                                     "partial re-run cannot associate one attempt's candidate with another attempt's checks — re-run ALL jobs "
                                     "of run %s so one attempt carries the candidate, its reconstruction and the aggregate check"
                                     % (job.get("name"), job.get("run_id"), job.get("run_attempt"), job.get("head_sha"), attempt, run_id)))
        elif job.get("status") != "completed" or job.get("conclusion") != "success":
            problems.append(_problem("certification_job_not_successful", "job %r of attempt %s is %s/%s" % (job.get("name"), attempt, job.get("status"), job.get("conclusion"))))
    for name in REQUIRED_JOBS:
        named = [j for j in job_list if isinstance(j, dict) and j.get("name") == name]
        if len(named) != 1:
            problems.append(_problem("certification_job_missing", "attempt %s lists %d %r jobs; exactly one must have succeeded in it" % (attempt, len(named), name)))
        else:
            selected_jobs.append({"name": name, "id": str(named[0].get("id")), "conclusion": named[0].get("conclusion"),
                                  "runAttempt": str(named[0].get("run_attempt"))})

    art_list = arts.get("artifacts")
    selected = {}
    if not isinstance(art_list, list) or arts.get("total_count") != len(art_list):
        problems.append(_problem("certification_listing_incomplete", "the artifact listing of run %s is unreadable or incomplete — a partial "
                                 "listing cannot prove which candidate exists" % run_id))
    else:
        if any(isinstance(a, dict) and a.get("name") == cd.artifact_name(False, run_id, attempt) for a in art_list):
            problems.append(_problem("certification_contradictory", "run %s attempt %s also lists a NON-promotable candidate" % (run_id, attempt)))
        for key, name in (("candidate", candidate_name(run_id, attempt)), ("reconstruction", reconstruction_name(run_id, attempt))):
            matches = [a for a in art_list if isinstance(a, dict) and a.get("name") == name]
            if not matches:
                others = sorted(str(a.get("name")) for a in art_list if isinstance(a, dict)
                                and str(a.get("name")).startswith(name.rsplit("-", 2)[0] + "-"))
                problems.append(_problem("certification_no_%s" % key, "run %s attempt %s retained no %s%s" % (run_id, attempt, name,
                                         " (it lists %s, which belong to other attempts and are not eligible)" % ", ".join(others) if others else "")))
            elif len(matches) > 1:
                problems.append(_problem("certification_ambiguous", "run %s lists %d artifacts named %s" % (run_id, len(matches), name)))
            else:
                selected[key] = _listed_artifact(matches[0], run_id, target, problems, name)

    commit = docs["commit"]
    tree = (commit.get("tree") or {}).get("sha")
    want("commit_unreadable", commit.get("sha") == target and _SHA.match(str(tree)), "the commit %s and its tree could not be read" % target)
    compare, main = docs["compare"], docs["main"]
    want("not_on_main", compare.get("status") in ("ahead", "identical") and (compare.get("merge_base_commit") or {}).get("sha") == target
         and _SHA.match(str(main.get("sha"))),
         "%s is not in current main's history (compare %s, merge base %s)" % (target, compare.get("status"), (compare.get("merge_base_commit") or {}).get("sha")))
    if problems:
        return None, problems
    return {"repository": repository, "commit": target, "tree": tree, "main": main["sha"],
            "workflow": {"id": str(wf["id"]), "path": wf["path"]},
            "run": {"id": run_id, "attempt": attempt, "event": run["event"], "headBranch": run["head_branch"], "conclusion": run["conclusion"],
                    "runStartedAt": run.get("run_started_at")},
            "jobs": selected_jobs, "candidate": selected["candidate"], "reconstruction": selected["reconstruction"]}, []


def trusted_trees(root, facts_dir, revision):
    """The verifier's own trees, three ways that must agree: its checkout's git, the API's
    tree of the evaluator revision, and the API's tree of current main. Returns
    ``(trees, problems)``. A mismatch with main means a NEW evaluation, never reuse."""
    problems, local = [], {}
    try:
        head = st.git(root, "rev-parse", "HEAD").strip()
        for name in TRUSTED_TREES:
            local[name] = st.git(root, "rev-parse", "HEAD:%s" % name).strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None, [_problem("evaluator_unverified", "the verifier's own checkout could not be read with git")]
    if head != revision:
        problems.append(_problem("evaluator_unverified", "the verifier checkout is %s, not the evaluator revision %s" % (head, revision)))

    def listed(key):
        doc, err = read_fact(facts_dir, key)
        entries = (doc or {}).get("tree")
        if err or not isinstance(entries, list):
            return None
        return {e.get("path"): e.get("sha") for e in entries if isinstance(e, dict) and e.get("type") == "tree"}

    stated, main = listed("evaluatorTree"), listed("mainTree")
    if stated is None or main is None:
        return None, problems + [_problem("evaluator_unverified", "the evaluator's or main's tree listing is unreadable")]
    for name in TRUSTED_TREES:
        if stated.get(name) != local[name]:
            problems.append(_problem("evaluator_unverified", "%s/ in the checkout is %s; the API states %s at %s" % (name, local[name], stated.get(name), revision)))
        if main.get(name) != local[name]:
            problems.append(_problem("evaluator_not_current", "%s/ on main is %s, the evaluator ran %s: the verifier or policy moved, so a "
                                     "coherent NEW evaluation is needed — an older answer is not repurposed" % (name, main.get(name), local[name])))
    return local, problems


# --- artifacts: admit by digest, THEN unpack ------------------------------------------------

def unpack(zip_path, digest, destination):
    """Refuse ``zip_path`` unless its sha256 is ``digest`` (the listing's), and only then
    extract its members under an EMPTY ``destination``: regular files and directories only,
    canonical relative names, each once, bounded sizes, never through a link."""
    if not _DIGEST.match(str(digest)):
        return [_problem("artifact_unsafe", "no valid expected digest for %s" % os.path.basename(zip_path))]
    try:
        actual = "sha256:" + cd.file_sha256(zip_path)
    except OSError as error:
        return [_problem("artifact_unreadable", "%s: %s" % (os.path.basename(zip_path), type(error).__name__))]
    if actual != digest:
        return [_problem("artifact_digest_mismatch", "%s is %s; the listing states %s — nothing was extracted" % (os.path.basename(zip_path), actual, digest))]
    problems, members, total = [], {}, 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return [_problem("artifact_unsafe", "%s holds %d members" % (os.path.basename(zip_path), len(infos)))]
            for info in infos:
                name, kind = info.filename, (info.external_attr >> 16) & 0o170000
                clean = name.rstrip("/") if info.is_dir() else name
                if (not clean or clean.startswith("/") or "\\" in clean or "\0" in clean or ":" in clean.split("/")[0]
                        or posixpath.normpath(clean) != clean or ".." in clean.split("/") or clean.startswith("./")):
                    problems.append(_problem("artifact_unsafe", "member %r has an unsafe or ambiguous name" % name))
                elif kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (info.flag_bits & 0x1):
                    problems.append(_problem("artifact_unsafe", "member %r is a link, a special file or encrypted" % name))
                elif clean in members:
                    problems.append(_problem("artifact_unsafe", "member %r appears more than once" % name))
                elif info.file_size > MAX_MEMBER_BYTES:
                    problems.append(_problem("artifact_unsafe", "member %r is implausibly large" % name))
                else:
                    members[clean] = info
                    total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                problems.append(_problem("artifact_unsafe", "%s would unpack to %d bytes" % (os.path.basename(zip_path), total)))
            files = [m for m in members if not members[m].is_dir()]
            for f in files:
                if any(g.startswith(f + "/") for g in members):
                    problems.append(_problem("artifact_unsafe", "%s is both a file and a directory" % f))
            if problems:
                return problems
            if os.path.lexists(destination) and (not os.path.isdir(destination) or os.listdir(destination)):
                return [_problem("artifact_unsafe", "%s must be an empty directory" % destination)]
            os.makedirs(destination, exist_ok=True)
            base = os.path.realpath(destination)
            for name in sorted(files):
                data = zf.read(members[name])          # CRC-checked by zipfile
                full = os.path.join(base, *name.split("/"))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                if not os.path.realpath(os.path.dirname(full)).startswith(base):
                    raise ValueError("%s escapes the destination" % name)
                fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            for name in members:
                if members[name].is_dir():
                    os.makedirs(os.path.join(base, *name.split("/")), exist_ok=True)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        return [_problem("artifact_unreadable", "%s: %s" % (os.path.basename(zip_path), error))]
    return []


# --- the reconstruction, bound to THIS record ------------------------------------------------

def reconstruction_problems(report_dir, record, record_sha256, selection):
    problems = []
    try:
        names = sorted(os.listdir(report_dir))
    except OSError:
        names = []
    if names != [RECONSTRUCTION_FILE]:
        return [_problem("reconstruction_invalid", "the reconstruction artifact holds %s, not exactly %s" % (names, RECONSTRUCTION_FILE))]
    try:
        with open(os.path.join(report_dir, RECONSTRUCTION_FILE), "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as error:
        return [_problem("reconstruction_invalid", "the reconstruction report is unreadable: %s" % type(error).__name__)]
    if not isinstance(report, dict) or report.get("schema") != cs.RECONSTRUCTION_SCHEMA:
        return [_problem("reconstruction_invalid", "the reconstruction report is not a %s document" % cs.RECONSTRUCTION_SCHEMA)]
    if report.get("verified") is not True or report.get("problems") != [] or not (report.get("startup") or {}).get("ok"):
        problems.append(_problem("reconstruction_failed", "the report does not record a verified, problem-free reconstruction that started both planes"))
    expected, c, env = report.get("expected") or {}, report.get("candidate") or {}, report.get("environment") or {}
    run = selection["run"]
    pairs = [("expected commit", expected.get("commit"), selection["commit"]), ("expected tree", expected.get("tree"), selection["tree"]),
             ("expected run", str(expected.get("runId")), run["id"]), ("expected attempt", str(expected.get("runAttempt")), run["attempt"]),
             ("expected event", expected.get("event"), "push"), ("expected ref", expected.get("ref"), cd.MAIN_REF),
             ("expected artifact", expected.get("artifact"), selection["candidate"]["name"]), ("expected repository", expected.get("repository"), cd.REPOSITORY),
             ("record", c.get("recordSha256"), record_sha256), ("artifact", c.get("artifact"), selection["candidate"]["name"]),
             ("commit", c.get("commit"), selection["commit"]), ("tree", c.get("tree"), selection["tree"]),
             ("run", str(c.get("run")), run["id"]), ("attempt", str(c.get("attempt")), run["attempt"]),
             ("environment digest", env.get("digest"), record["environment"]["digest"]), ("environment matches its record", env.get("matchesRecord"), True)]
    for label, actual, wanted in pairs:
        if actual != wanted:
            problems.append(_problem("reconstruction_foreign", "the reconstruction's %s is %r; this candidate's is %r" % (label, actual, wanted)))
    target = report.get("target") or {}
    for key in ("python", "implementation", "platform", "machine"):
        if (target.get("reconstructed") or {}).get(key) != (record.get("target") or {}).get(key):
            problems.append(_problem("reconstruction_foreign", "the reconstruction ran with %s %r, the record certifies %r"
                                     % (key, (target.get("reconstructed") or {}).get(key), (record.get("target") or {}).get(key))))
    return problems


# --- the retained inventory, and the scanner's environment ---------------------------------

def retained_inventory(candidate_dir, record):
    """The application graph to query: the certification snapshot's exact name/version set,
    classified by the TRUSTED scope rule. Returns ``(packages, problems)``."""
    from dependency_audit import orchestrate as oc, pip_adapter as pa
    snap, problem = oc._read_evidence(os.path.join(candidate_dir, cd.EVIDENCE, "snapshot.json"), oc.SNAPSHOT_SCHEMA)
    if problem:
        return None, [_problem("inventory_unbound", problem)]
    packages = []
    for p in snap.get("packages") or []:
        if not (isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("version"), str)):
            return None, [_problem("inventory_unbound", "the retained snapshot lists a malformed package")]
        pkg = {"name": p["name"], "version": p["version"], "recordSha256": p.get("recordSha256"),
               "path": "application:site-packages/%s" % p["name"]}
        pkg["scope"] = pa.application_scope(pkg)
        packages.append(pkg)
    packages.sort(key=lambda p: p["name"])
    if not packages or pa.inventory_digest(packages) != (record.get("environment") or {}).get("auditInventorySha256"):
        return None, [_problem("inventory_unbound", "the retained inventory is not the one the record certified")]
    return packages, []


def withhold_runner_authority(environ):
    """Remove the runner's step-output files and every token from ``environ`` (in place),
    returning what was removed so the CLI alone can write its outputs afterwards."""
    removed = {}
    for key in list(environ):
        if key in RUNNER_AUTHORITY or key.startswith("ACTIONS_") or key.upper().endswith("_TOKEN"):
            removed[key] = environ.pop(key)
    return removed


def scanner_runner(runner):
    """Every process the scanner path starts — venv creation, the hash-pinned install, pip
    inspect, pip-audit — gets the B2.1 scrubbed environment WITHOUT runner authority, and
    ``PIP_CONFIG_FILE=/dev/null``: ``--isolated`` alone still reads the global and site
    ``pip.conf`` (measured in B2.5), so configuration cannot add an index or a find-links
    location to this bootstrap. The B2.1 CI install is deliberately not changed here."""
    from dependency_audit import pip_adapter as pa

    def run(command, args, cwd=None, env=None, timeout=None):
        base = dict(env if env is not None else pa.scrubbed_environment()[0])
        for key in list(base):
            if key in RUNNER_AUTHORITY or key.startswith("ACTIONS_") or key.upper().endswith("_TOKEN"):
                base.pop(key)
        base["PIP_CONFIG_FILE"] = os.devnull
        return runner(command, args, cwd=cwd, env=base, timeout=timeout)
    return run


def applied_records(result, records):
    applied = [r["id"] for r in result["records"] if r["status"] == "applied" and r["covers"] > 0]
    by_id = {r.get("id"): r for r in records if isinstance(r, dict)}
    return [{"id": i, "kind": by_id.get(i, {}).get("kind"), "expires": by_id.get(i, {}).get("expires")} for i in applied]


def deadline_ms(started_at, recorded):
    from dependency_audit import core
    limits = [_ms(started_at) + ASSESSMENT_WINDOW_HOURS * 3600 * 1000] if _ms(started_at) is not None else []
    for r in recorded:
        lapse = core.parse_date(r.get("expires"))
        limits.append(lapse if lapse is not None else -1)
    return min(limits) if limits else None


def _candidate_facts(candidate_dir, record, selection):
    audit = record.get("audit") or {}
    return {"artifact": selection["candidate"], "recordSha256": cd.file_sha256(os.path.join(candidate_dir, cd.RECORD)),
            "createdAt": record.get("createdAt"),
            "source": {"archiveSha256": record["source"]["archive"]["sha256"], "tree": record["tree"], "contentSha256": record["source"]["contentSha256"]},
            "wheelhouseDigest": record["wheelhouse"]["digest"], "lockSha256": record["inputs"]["lock"]["sha256"],
            "requirementsSha256": record["inputs"]["requirements"]["sha256"], "environmentDigest": record["environment"]["digest"],
            "auditInventorySha256": record["environment"]["auditInventorySha256"],
            "originalAudit": {"outcome": audit.get("outcome"), "decidedAt": audit.get("decidedAt"), "collectionStartedAt": audit.get("collectionStartedAt"),
                              "collectionFinishedAt": audit.get("collectionFinishedAt"), "policySha256": audit.get("policySha256"),
                              "evidenceDigest": (audit.get("evidence") or {}).get("digest"),
                              "note": "historical certification evidence, verified as recorded under the policy the candidate carries; "
                                      "it is not re-dated and does not decide this preflight"}}


def _verified_candidate(facts_dir, selection, work):
    """Unpack the candidate and reconstruction zips by digest, verify the candidate with
    the B2.5 consumer (nothing in it executes) and bind the reconstruction to its record."""
    candidate, recon = os.path.join(work, "candidate"), os.path.join(work, "reconstruction")
    problems = unpack(os.path.join(facts_dir, ZIPS, "candidate.zip"), selection["candidate"]["digest"], candidate)
    problems += unpack(os.path.join(facts_dir, ZIPS, "reconstruction.zip"), selection["reconstruction"]["digest"], recon)
    if problems:
        return None, None, None, problems
    expect = {"repository": selection["repository"], "commit": selection["commit"], "tree": selection["tree"], "runId": selection["run"]["id"],
              "runAttempt": selection["run"]["attempt"], "event": "push", "ref": cd.MAIN_REF, "artifact": selection["candidate"]["name"],
              "workflowPath": cd.WORKFLOW_PATH, "local": False}
    state, problems = cs.verify(candidate, expect)
    if problems:
        return None, None, None, problems
    problems = reconstruction_problems(recon, state["record"], cd.file_sha256(os.path.join(candidate, cd.RECORD)), selection)
    if problems:
        return None, None, None, problems
    return candidate, state, recon, []


def _write_doc(out, doc):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, PREFLIGHT_DOC), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return doc


# --- the assessment ------------------------------------------------------------------------

def assess(root, facts_dir, out, work, evaluation, clock, runner=None, install_scanner=None, withheld=()):
    """Select, verify, and query NOW. Writes ``out/preflight.json`` and the raw scanner
    output, whatever the outcome; returns the document. ``evaluation`` is
    ``{runId, runAttempt, revision}`` — this run's own identity. ``clock`` is the only time
    source; the CLI passes the real one."""
    from dependency_audit import core, orchestrate as oc, pip_adapter as pa
    runner = runner or oc.spawn_runner
    install_scanner = install_scanner or oc.default_install_scanner
    started = clock()
    if os.path.lexists(out):
        raise ValueError("%s already exists; a preflight result is only ever written fresh" % out)
    doc = {"schema": PREFLIGHT_SCHEMA, "scope": dict(SCOPE), "startedAt": started, "decision": "refused", "problems": [],
           "evaluator": {"workflowPath": PREFLIGHT_WORKFLOW_PATH, "runId": str(evaluation.get("runId")),
                         "runAttempt": str(evaluation.get("runAttempt")), "revision": evaluation.get("revision")},
           "assessment": None, "deadline": None}
    choice, err = read_fact(facts_dir, "choice")
    if err:
        doc["problems"] = [err]
        return _write_doc(out, doc)
    doc["request"] = {k: choice.get(k) for k in ("target", "runId", "runAttempt", "source")}
    if choice.get("revision") != evaluation.get("revision"):
        doc["problems"] = [_problem("evaluator_unverified", "the facts were read for revision %s, this evaluation is %s" % (choice.get("revision"), evaluation.get("revision")))]
        return _write_doc(out, doc)
    selection, problems = select(facts_dir, choice)
    trees, tree_problems = trusted_trees(root, facts_dir, evaluation.get("revision"))
    problems += tree_problems
    if problems:
        doc["problems"] = problems
        return _write_doc(out, doc)
    doc.update(repository=selection["repository"], commit=selection["commit"], tree=selection["tree"],
               certification={k: selection[k] for k in ("workflow", "run", "jobs", "main")})
    os.makedirs(work, exist_ok=True)
    candidate, state, recon, problems = _verified_candidate(facts_dir, selection, work)
    if problems:
        doc["problems"] = problems
        return _write_doc(out, doc)
    record = state["record"]
    doc["candidate"] = _candidate_facts(candidate, record, selection)
    with open(os.path.join(recon, RECONSTRUCTION_FILE), "rb") as fh:
        recon_bytes = fh.read()
    doc["reconstruction"] = {"artifact": selection["reconstruction"], "reportSha256": _sha256(recon_bytes),
                             "reconstructedAt": json.loads(recon_bytes.decode("utf-8")).get("reconstructedAt"),
                             "environmentDigest": record["environment"]["digest"]}
    packages, problems = retained_inventory(candidate, record)
    policy, policy_problems = oc.load_policy(root)
    if problems or policy_problems:
        doc["problems"] = problems + policy_problems
        return _write_doc(out, doc)
    with open(os.path.join(root, "dependency_audit", "policy.json"), "rb") as fh:
        policy_sha = _sha256(fh.read())
    with open(os.path.join(root, policy["scanner"]["requirements"]), "rb") as fh:
        scanner_req_sha = _sha256(fh.read())
    doc["evaluator"].update(trees=trees, policySha256=policy_sha, scannerRequirementsSha256=scanner_req_sha,
                            scanner={"package": policy["scanner"]["package"], "version": policy["scanner"]["version"]})

    # THE QUERY. Everything after this point is the fresh advisory question and nothing else.
    os.makedirs(out, exist_ok=True)
    isolated = scanner_runner(runner)
    incomplete, findings, graphs = [], [], {}
    workdir = tempfile.mkdtemp(prefix="preflight-scanner-", dir=work)
    try:
        scanner_python, problems, summary = install_scanner(root, policy, isolated, workdir)
        incomplete += problems
        scanner_packages = []
        if not incomplete:
            scanner_packages, problems = oc.inspect_environment(scanner_python, "scanner", pa.tooling_scope, isolated)
            incomplete += problems + oc.verify_scanner(root, policy, scanner_packages)
        doc["evaluator"]["scanner"].update(install=summary, inventorySha256=pa.inventory_digest(scanner_packages) if scanner_packages else None)
        if not incomplete:
            pip_audit = os.path.join(os.path.dirname(scanner_python), "pip-audit")
            for graph, pkgs, observation in (("application", packages, "retained-inventory"), ("scanner", scanner_packages, "installed-now")):
                reqs = os.path.join(out, "%s.inventory-requirements.txt" % graph)
                with open(reqs, "w", encoding="utf-8") as fh:
                    fh.write(pa.pins_text(pkgs))
                cache = tempfile.mkdtemp(prefix="advisory-cache-", dir=workdir)
                query_started = clock()
                run = isolated(pip_audit, pa.scanner_args(reqs, cache, 30), timeout=policy["scanner"]["timeoutSeconds"])
                query_finished = clock()
                shutil.rmtree(cache, ignore_errors=True)
                if graph == "scanner":
                    now_pkgs, _ = oc.inspect_environment(scanner_python, "scanner", pa.tooling_scope, isolated)
                    if pa.inventory_digest(now_pkgs) != pa.inventory_digest(pkgs):
                        incomplete.append({"code": "inventory_changed_during_audit", "detail": "scanner: the scanner environment changed while it was scanning"})
                problems, found = pa.read_report(graph, run, pkgs)
                incomplete += problems
                findings += found
                graphs[graph] = {"observation": observation, "queryStartedAt": query_started, "queryFinishedAt": query_finished,
                                 "inventorySha256": pa.inventory_digest(pkgs), "installed": len(pkgs),
                                 "requirementsSha256": _sha256(pa.pins_text(pkgs)), "run": oc._record_run(out, graph, run),
                                 "packages": [{"name": p["name"], "version": p["version"], "scope": p["scope"], "recordSha256": p["recordSha256"]} for p in pkgs]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    finished = clock()
    decided = clock()
    result = core.evaluate(incomplete, findings, policy.get("records") or [], decided)
    recorded = applied_records(result, policy.get("records") or [])
    limit = deadline_ms(started, recorded)
    doc["assessment"] = {"finishedAt": finished, "decidedAt": decided, "graphs": graphs, "freshness": FRESHNESS,
                         "removedEnvironment": sorted(set(withheld) | set(pa.scrubbed_environment()[1])),
                         "outcome": result["outcome"], "exitCode": result["exitCode"], "counts": result["counts"],
                         "headline": core.headline(result), "reasons": result["reasons"], "recordsApplied": recorded,
                         "windowHours": ASSESSMENT_WINDOW_HOURS}
    doc["deadline"] = _iso(limit) if limit is not None and limit > 0 else None
    doc["decision"] = "accepted" if result["outcome"] in PASSING and result["exitCode"] == 0 and doc["deadline"] else \
        ("incomplete" if result["outcome"] == "incomplete" else "blocking")
    return _write_doc(out, doc)


def listing_digest(value):
    """One spelling for an artifact digest: ``sha256:<hex>``, as the REST listing states it.
    ``actions/upload-artifact``'s ``artifact-digest`` output is the BARE hex of the same
    digest, and the workflow forwards that output as the receiving check's hint, so both
    spellings name one artifact. Anything else is not a digest and gives ``None``."""
    text = value if isinstance(value, str) else ""
    if _HEX.match(text):
        return "sha256:" + text
    return text if _DIGEST.match(text) else None


def exit_code(doc):
    return {"accepted": 0, "incomplete": 2}.get(doc.get("decision"), 1)


# --- the receiving side --------------------------------------------------------------------

def evaluation_run(facts_dir, evaluation, repository=cd.REPOSITORY):
    """The evaluation run as the API states it: a run of THIS workflow, this id and attempt."""
    run, err = read_fact(facts_dir, "evaluationRun")
    if err:
        return None, [err]
    if run.get("path") != PREFLIGHT_WORKFLOW_PATH or str(run.get("id")) != str(evaluation["runId"]) \
            or str(run.get("run_attempt")) != str(evaluation["runAttempt"]) or _ms(run.get("run_started_at")) is None \
            or (run.get("repository") or {}).get("full_name") != repository or not _SHA.match(str(run.get("head_sha"))):
        return None, [_problem("preflight_wrong_evaluation", "run %s attempt %s is not a %s run of %s" % (evaluation["runId"], evaluation["runAttempt"],
                                                                                                     PREFLIGHT_WORKFLOW_PATH, repository))]
    return run, []


def find_preflight(facts_dir, evaluation):
    """The result artifact, as the evaluation run's OWN listing states it."""
    run, problems = evaluation_run(facts_dir, evaluation)
    if problems:
        return None, problems
    arts, err = read_fact(facts_dir, "evaluationArtifacts")
    if err:
        return None, [err]
    listed = arts.get("artifacts")
    if not isinstance(listed, list) or arts.get("total_count") != len(listed):
        return None, [_problem("preflight_missing", "the evaluation run's artifact listing is unreadable or incomplete")]
    name = preflight_name(evaluation["runId"], evaluation["runAttempt"])
    matches = [a for a in listed if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        return None, [_problem("preflight_missing", "evaluation run %s lists %d artifacts named %s" % (evaluation["runId"], len(matches), name))]
    problems = []
    found = _listed_artifact(matches[0], evaluation["runId"], run["head_sha"], problems, name)
    return (dict(found, evaluationRun=run) if not problems else None), problems


def reproduce(root, result_dir, doc, candidate, record, stop_after_binding=False):
    """THE RAW OUTPUT IS THE EVIDENCE: the trusted policy and scanner at ``root`` are the
    ones the result names, the queried inventories are exactly the candidate's retained one
    and the trusted scanner's, the raw bytes are the recorded ones, and the decision
    reproduces under the trusted policy. Shared by the receiving check here and by the host
    installer (release/installation.py), which has no git checkout and no API facts but
    must reach the same answer from the same bytes. Returns ``(reproduced, problems)``."""
    from dependency_audit import core, orchestrate as oc, pip_adapter as pa
    problems = []
    a, ev = doc.get("assessment") or {}, doc.get("evaluator") or {}
    graphs = a.get("graphs") if isinstance(a.get("graphs"), dict) else {}
    policy, policy_problems = oc.load_policy(root)
    if policy_problems:
        return None, [_problem("preflight_policy_changed", p["detail"]) for p in policy_problems]
    with open(os.path.join(root, "dependency_audit", "policy.json"), "rb") as fh:
        if ev.get("policySha256") != _sha256(fh.read()):
            problems.append(_problem("preflight_policy_changed", "the result was decided under a different audit policy than the trusted one"))
    with open(os.path.join(root, policy["scanner"]["requirements"]), "rb") as fh:
        scanner_pins = pa.declared_requirements(fh.read().decode("utf-8"))
        fh.seek(0)
        if ev.get("scannerRequirementsSha256") != _sha256(fh.read()) or (ev.get("scanner") or {}).get("version") != policy["scanner"]["version"]:
            problems.append(_problem("preflight_wrong_scanner", "the result's scanner is not the trusted pinned scanner"))
    if problems or stop_after_binding:
        return None, problems

    # The raw output IS the evidence: exact files, exact inventories, and the decision reproduced.
    packages, problems = retained_inventory(candidate, record)
    if problems:
        return None, problems
    incomplete, findings = [], []
    for graph in GRAPHS:
        g = graphs.get(graph) if isinstance(graphs.get(graph), dict) else {}
        recorded = oc._recorded_run(g)
        if recorded is None or recorded.get("stdoutFile") != "%s.scanner-stdout.txt" % graph or recorded.get("stderrFile") != "%s.scanner-stderr.txt" % graph:
            problems.append(_problem("preflight_invalid", "%s: no recorded query" % graph))
            continue
        with open(os.path.join(result_dir, recorded["stdoutFile"]), "rb") as fh:
            stdout = fh.read()
        with open(os.path.join(result_dir, recorded["stderrFile"]), "rb") as fh:
            stderr = fh.read()
        with open(os.path.join(result_dir, "%s.inventory-requirements.txt" % graph), "rb") as fh:
            reqs = fh.read()
        if _sha256(stdout) != recorded["stdoutSha256"] or _sha256(stderr) != recorded.get("stderrSha256"):
            problems.append(_problem("preflight_raw_mismatch", "%s: the raw scanner output is not the bytes the result recorded" % graph))
            continue
        if graph == "application":
            pkgs = packages
            if g.get("observation") != "retained-inventory":
                problems.append(_problem("preflight_wrong_inventory", "application: the query was not over the retained inventory"))
        else:
            listed = oc._recorded_packages(g)
            if listed is None or {p["name"]: p["version"] for p in listed} != scanner_pins or g.get("observation") != "installed-now":
                problems.append(_problem("preflight_wrong_scanner", "scanner: the queried scanner graph is not the trusted pinned set"))
                continue
            pkgs = [dict(p, path="scanner:site-packages/%s" % p["name"], scope=pa.tooling_scope(p)) for p in listed]
        if reqs != pa.pins_text(pkgs).encode("utf-8") or g.get("inventorySha256") != pa.inventory_digest(pkgs) \
                or g.get("requirementsSha256") != _sha256(reqs):
            problems.append(_problem("preflight_wrong_inventory", "%s: the query did not cover exactly this inventory" % graph))
            continue
        found_problems, found_findings = pa.read_report(graph, dict(recorded, stdout=stdout.decode("utf-8")), pkgs)
        incomplete += found_problems
        findings += found_findings
    if problems:
        return None, problems
    reproduced = core.evaluate(incomplete, findings, policy.get("records") or [], a.get("decidedAt"))
    if reproduced["outcome"] != a.get("outcome") or reproduced["counts"] != a.get("counts") \
            or applied_records(reproduced, policy.get("records") or []) != a.get("recordsApplied"):
        problems.append(_problem("preflight_unreproducible", "the raw output decides %s under the trusted policy; the result says %s"
                                 % (reproduced["outcome"], a.get("outcome"))))
    if reproduced["outcome"] not in PASSING or reproduced["exitCode"] != 0:
        problems.append(_problem("preflight_not_accepted", core.headline(reproduced)))

    if problems:
        return None, problems
    return reproduced, []


def receive(root, facts_dir, work, evaluation, now, margin_minutes=RECEIVING_MARGIN_MINUTES, expect_preflight=None):
    """THE RECEIVING CHECK. Everything is re-derived here: the selection from this side's own
    facts, the result from the evaluation run's own listing, the candidate's bytes through
    the consumer, the decision from the raw output under THIS side's trusted policy. Returns
    ``(admitted, problems)``; ``admitted`` holds only bounded, validated values."""
    from dependency_audit import core, orchestrate as oc, pip_adapter as pa
    problems = []
    choice, err = read_fact(facts_dir, "choice")
    if err:
        return None, [err]
    selection, problems = select(facts_dir, choice)
    if problems:
        return None, problems
    found, problems = find_preflight(facts_dir, evaluation)
    if problems:
        return None, problems
    run = found.pop("evaluationRun")
    if expect_preflight and (expect_preflight.get("id") != found["id"] or listing_digest(expect_preflight.get("digest")) != found["digest"]):
        return None, [_problem("preflight_wrong_evaluation", "the listing names artifact %s (%s); the assessing job reported %s (%s)"
                               % (found["id"], found["digest"], expect_preflight.get("id"), expect_preflight.get("digest")))]
    os.makedirs(work, exist_ok=True)
    result_dir = os.path.join(work, "preflight")
    problems = unpack(os.path.join(facts_dir, ZIPS, "preflight.zip"), found["digest"], result_dir)
    if problems:
        return None, problems
    try:
        with open(os.path.join(result_dir, PREFLIGHT_DOC), "rb") as fh:
            doc_bytes = fh.read()
        doc = json.loads(doc_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        return None, [_problem("preflight_invalid", "%s is unreadable: %s" % (PREFLIGHT_DOC, type(error).__name__))]
    if not isinstance(doc, dict) or doc.get("schema") != PREFLIGHT_SCHEMA:
        return None, [_problem("preflight_invalid", "the result is not a %s document" % PREFLIGHT_SCHEMA)]
    if doc.get("scope") != SCOPE:
        return None, [_problem("preflight_scope_invalid", "the result's scope is not the non-deploying preflight scope")]
    if doc.get("decision") != "accepted" or not isinstance(doc.get("assessment"), dict):
        return None, [_problem("preflight_not_accepted", "the result decided %r: %s" % (doc.get("decision"), "; ".join(p.get("detail", "") for p in doc.get("problems") or [])[:500]))]
    a, ev = doc["assessment"], doc.get("evaluator") or {}
    graphs = a.get("graphs") if isinstance(a.get("graphs"), dict) else {}
    expected_files = {PREFLIGHT_DOC} | set(RAW_FILES)
    present = set()
    for dirpath, dirnames, filenames in os.walk(result_dir):
        for f in filenames:
            present.add(os.path.relpath(os.path.join(dirpath, f), result_dir).replace(os.sep, "/"))
    if present != expected_files:
        problems.append(_problem("preflight_unexpected_file", "the result holds %s; expected exactly %s" % (sorted(present), sorted(expected_files))))

    # Bound to THIS certification and THIS candidate.
    for label, actual, wanted in (("repository", doc.get("repository"), selection["repository"]), ("commit", doc.get("commit"), selection["commit"]),
                                  ("tree", doc.get("tree"), selection["tree"])):
        if actual != wanted:
            problems.append(_problem("preflight_wrong_candidate", "the result's %s is %r, the selected candidate's %r" % (label, actual, wanted)))
    cert = doc.get("certification") or {}
    if cert.get("run") != selection["run"] or cert.get("jobs") != selection["jobs"] or cert.get("workflow") != selection["workflow"]:
        problems.append(_problem("preflight_wrong_certification", "the result names a different certifying run, attempt or job set"))
    if (doc.get("reconstruction") or {}).get("artifact") != selection["reconstruction"] or (doc.get("candidate") or {}).get("artifact") != selection["candidate"]:
        problems.append(_problem("preflight_wrong_candidate", "the result names other artifacts than the certification's listing"))
    if problems:
        return None, problems
    candidate, state, recon, problems = _verified_candidate(facts_dir, selection, os.path.join(work, "received"))
    if problems:
        return None, problems
    record = state["record"]
    derived = _candidate_facts(candidate, record, selection)
    if doc.get("candidate") != derived:
        problems.append(_problem("preflight_wrong_candidate", "the result does not describe these candidate bytes (record, archive, wheelhouse, "
                                 "lock, environment or original audit differ)"))
    with open(os.path.join(recon, RECONSTRUCTION_FILE), "rb") as fh:
        if (doc.get("reconstruction") or {}).get("reportSha256") != _sha256(fh.read()):
            problems.append(_problem("preflight_wrong_candidate", "the result names another reconstruction report"))

    # Bound to THIS evaluator and THIS policy — both still what main carries.
    if ev.get("runId") != str(evaluation["runId"]) or ev.get("runAttempt") != str(evaluation["runAttempt"]) \
            or ev.get("workflowPath") != PREFLIGHT_WORKFLOW_PATH or ev.get("revision") != run.get("head_sha"):
        problems.append(_problem("preflight_wrong_evaluation", "the result was written by another evaluation"))
    trees, tree_problems = trusted_trees(root, facts_dir, choice.get("revision"))
    if tree_problems or ev.get("trees") != trees:
        problems.append(_problem("preflight_evaluator_changed", "the result's evaluator (%s, trees %s) is not this verifier and main's: %s"
                                 % (ev.get("revision"), ev.get("trees"), "; ".join(p["detail"] for p in tree_problems) or "trees differ")))
    reproduced, repro_problems = reproduce(root, result_dir, doc, candidate, record, stop_after_binding=bool(problems))
    problems += repro_problems
    if problems:
        return None, problems

    # TIME: ordered, inside GitHub's own bounds, and not expired (with the margin).
    times = [doc.get("startedAt")] + [graphs[g].get(k) for g in GRAPHS for k in ("queryStartedAt", "queryFinishedAt")] + [a.get("finishedAt"), a.get("decidedAt")]
    ms = [_ms(t) for t in times]
    skew = CLOCK_SKEW_SECONDS * 1000
    now_ms = _ms(now)
    if any(m is None for m in ms) or now_ms is None or ms != sorted(ms):
        problems.append(_problem("preflight_time_invalid", "the result's times are missing or out of order"))
    else:
        if ms[0] < _ms(run["run_started_at"]) - skew:
            problems.append(_problem("preflight_time_invalid", "the result claims its evaluation began %s, before run %s started at %s"
                                     % (doc["startedAt"], evaluation["runId"], run["run_started_at"])))
        created = _ms(found.get("createdAt"))
        if created is None or ms[-1] > created + skew:
            problems.append(_problem("preflight_time_invalid", "the result claims a decision at %s, after GitHub recorded its upload at %s"
                                     % (a.get("decidedAt"), found.get("createdAt"))))
        if ms[-1] > now_ms + skew:
            problems.append(_problem("preflight_time_invalid", "the result was decided in the future (%s; now %s)" % (a.get("decidedAt"), now)))
        limit = deadline_ms(doc["startedAt"], a.get("recordsApplied") or [])
        if limit is None or doc.get("deadline") != _iso(limit):
            problems.append(_problem("preflight_time_invalid", "the result's deadline %s is not the one its window and records give" % doc.get("deadline")))
        elif now_ms + margin_minutes * 60 * 1000 >= limit:
            problems.append(_problem("preflight_expired", "the assessment (begun %s) or an applied record stops authorising anything at %s; with a "
                                     "%d-minute margin it cannot be relied on at %s — a NEW preflight is needed" % (doc["startedAt"], doc["deadline"], margin_minutes, now)))
    if problems:
        return None, problems
    return {"commit": selection["commit"], "tree": selection["tree"], "ciRun": selection["run"]["id"], "ciAttempt": selection["run"]["attempt"],
            "candidateArtifactId": selection["candidate"]["id"], "candidateDigest": selection["candidate"]["digest"],
            "recordSha256": derived["recordSha256"], "environmentDigest": derived["environmentDigest"],
            "preflightArtifactId": found["id"], "preflightDigest": found["digest"], "preflightSha256": _sha256(doc_bytes),
            "outcome": reproduced["outcome"], "deadline": doc["deadline"], "deadlineEpoch": deadline_ms(doc["startedAt"], a.get("recordsApplied") or []) // 1000,
            "deploymentAuthorized": False}, []
