"""release — the Backend candidate producer and consumer. See release/README.md.

    python -B -m release lock check                     offline; the lock agrees with requirements.txt
    python -B -m release lock generate                  NETWORK; maintainers only, then review the diff
    python -B -m release observe --out FILE             offline; the checkout IS the commit, nothing else exists
    python -B -m release acquire --wheelhouse DIR       NETWORK (PyPI files only); exactly the locked files
    python -B -m release install --wheelhouse DIR --venv DIR --work DIR
                                                        offline; a fresh environment from those files, reconciled
    python -B -m release interpreter --venv DIR         offline; the running python IS that environment
    python -B -m release package --wheelhouse DIR --venv DIR --evidence DIR --before FILE --out DIR [--local --step-outcomes FILE]
                                                        offline; after every required step passed
    python -B -m release verify|reconstruct --candidate DIR --expect-commit SHA [...]
                                                        the non-privileged consumer (reconstruct also needs --work)
    python -B -m release preflight facts --out DIR --revision SHA [--evaluation-run R --evaluation-attempt A]
                                                        READ TOKEN (gh); the request comes from PREFLIGHT_* variables
    python -B -m release preflight assess --facts DIR --out DIR --work DIR --evaluation-run R --evaluation-attempt A --revision SHA
                                                        NO TOKEN; NETWORK (the pinned scanner, PyPI); a fresh query now
    python -B -m release preflight verify --facts DIR --work DIR --evaluation-run R --evaluation-attempt A [--admission-out FILE] [...]
                                                        NO TOKEN; the receiving check (release/preflight.py); the
                                                        installer's admission document when asked
    python -B -m release host validate-profile --profile FILE [--rehearsal]
                                                        offline; the installation profile, with every unknown field named
    python -B -m release host deploy --profile FILE --operation ID --admission FILE --candidate-zip ZIP --preflight-zip ZIP --trusted DIR
                                                        ROOT, on the host: admit, install beside what serves, gate, switch
                                                        both planes, verify, restore on failure (release/transition.py)
    python -B -m release host status|resume|adopt-legacy|recover-legacy --profile FILE [--operation ID]
    python -B -m release host resume --profile FILE --operation ID [--schema-established STATEMENT]
                                                        ROOT, on the host; see release/README.md -> "The installation"
    python -B -m release host build|reconcile ...       the UNPRIVILEGED preparation worker; the installer runs it

Exit status: 0 success · 1 refused (the reasons are printed) · 2 a preflight assessment that
could not be completed · 3 a transition that failed verification and was RESTORED · 4 a
transition whose restoration failed or could not be established · 64 usage. Run every command
with ``-B``: the producer's own bytecode must not appear in the checkout it is observing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

from . import candidate as cd
from . import consumer as cs
from . import environment as ev
from . import lockfile as lf
from . import preflight as pf
from . import producer as pr
from . import sourcetree as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def report(label, problems, ok_line):
    if problems:
        for p in problems:
            print("  ✗ %s: %s" % (p["code"], p["detail"]), file=sys.stderr)
        print("%s REFUSED (%d problem%s)." % (label, len(problems), "" if len(problems) == 1 else "s"), file=sys.stderr)
        return 1
    print(ok_line)
    return 0


def summary(markdown, environ=None):
    target = (os.environ if environ is None else environ).get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def output(key, value, environ=None):
    target = (os.environ if environ is None else environ).get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (key, value))


def cmd_lock(args):
    if args.action == "check":
        lock, _, _, problems = pr.read_inputs(ROOT)
        ok = "lock check: %s agrees with %s — %d application packages + %s %s (bootstrap), resolved %s" % (
            lf.LOCK_PATH, lf.REQUIREMENTS_PATH, len(lock["packages"]), lock["bootstrap"][0]["name"],
            lock["bootstrap"][0]["version"], lock["generator"]["resolvedAt"]) if lock else ""
        return report("LOCK CHECK", problems, ok)
    workdir = args.work or os.path.join(ROOT, ".release-generate")
    lock, problems = ev.generate(ROOT, workdir, now().split(".")[0] + "Z")
    if lock is not None:
        with open(os.path.join(ROOT, lf.LOCK_PATH), "wb") as fh:
            fh.write(lf.lock_bytes(lock))
    return report("LOCK GENERATE", problems, "wrote %s (%d packages); review the diff before committing" % (lf.LOCK_PATH, len((lock or {}).get("packages") or [])))


def cmd_observe(args):
    observation = st.observe(ROOT, st.BEFORE_VALIDATION, expected_commit=os.environ.get("GITHUB_SHA") or None, now=now())
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(observation, fh, indent=2, sort_keys=True)
    return report("SOURCE OBSERVATION", observation["problems"],
                  "source observation: %s (tree %s), %d tracked files byte-identical to the commit, nothing untracked"
                  % (observation.get("commit"), observation.get("tree"), observation.get("trackedFiles", 0)))


def cmd_acquire(args):
    lock, _, _, problems = pr.read_inputs(ROOT)
    if problems:
        return report("ACQUISITION", problems, "")
    listing, problems = ev.acquire(lock, args.wheelhouse)
    total = sum(e["size"] for e in listing)
    return report("ACQUISITION", problems, "acquired %d locked files (%d bytes), every size and sha256 exactly the lock's; wheelhouse digest %s"
                  % (len(listing), total, cd.listing_digest(listing)))


def cmd_install(args):
    lock, _, requirements_bytes, problems = pr.read_inputs(ROOT)
    if problems:
        return report("INSTALL", problems, "")
    install, problems = ev.create_environment(lock, args.wheelhouse, args.venv, args.work)
    inventory = None
    if not problems:
        pins, _ = lf.parse_direct_inputs(requirements_bytes.decode("utf-8"))
        inventory, problems = ev.reconcile(lock, args.wheelhouse, args.venv, pins)
        if inventory is not None:
            problems += ev.target_problems(lock, inventory["facts"])
    os.makedirs(args.work, exist_ok=True)
    with open(os.path.join(args.work, "install.json"), "w", encoding="utf-8") as fh:
        json.dump({"install": install, "inventory": inventory, "problems": problems}, fh, indent=2, sort_keys=True)
    ok = ""
    if inventory:
        ok = "certified environment: %d distributions installed offline from the wheelhouse, reconciled with the lock and every wheel's RECORD; pip check ok; portable inventory %s" % (
            inventory["installed"], inventory["digest"])
    return report("INSTALL", problems, ok)


def cmd_interpreter(args):
    return report("INTERPRETER", pr.interpreter_problems(args.venv), "interpreter: %s is the certified environment's" % sys.executable)


def cmd_package(args):
    # The steps' recorded outcomes are the evidence that validation passed, so an
    # unreadable or malformed record of them is a named refusal, never a crash (a crash
    # also exits non-zero, but says nothing about why nothing was certified).
    outcomes = None
    try:
        if args.step_outcomes:
            with open(args.step_outcomes, "r", encoding="utf-8") as fh:
                outcomes = json.load(fh)
        elif os.environ.get("STEP_OUTCOMES"):
            outcomes = json.loads(os.environ["STEP_OUTCOMES"])
    except (OSError, ValueError) as error:
        return report("PACKAGING", [{"code": "step_outcomes_unreadable", "detail": "the validation steps' recorded outcomes could not "
                                     "be read (%s); nothing is certified without them" % type(error).__name__}], "")
    if outcomes is not None and not isinstance(outcomes, dict):
        return report("PACKAGING", [{"code": "step_outcomes_unreadable", "detail": "the validation steps' recorded outcomes are a %s, "
                                     "not an object keyed by step id" % type(outcomes).__name__}], "")
    record, problems = pr.package(ROOT, args.wheelhouse, args.venv, args.evidence, args.before, args.out, now(), outcomes, local=args.local)
    if record:
        output("artifact", record["artifact"]["name"])
        output("promotable", "true" if record["eligibility"]["promotable"] else "false")
        summary("### Backend candidate\n\n| | |\n|---|---|\n| artifact | `%s` |\n| promotable | %s — %s |\n| commit / tree | `%s` / `%s` |\n"
                "| source.tar | `%s` |\n| wheelhouse | %d files, `%s` |\n| environment | %d installed, `%s` |\n| audit | %s |\n"
                % (record["artifact"]["name"], record["eligibility"]["promotable"], record["eligibility"]["reason"], record["commit"],
                   record["tree"], record["source"]["archive"]["sha256"], len(record["wheelhouse"]["files"]), record["wheelhouse"]["digest"],
                   record["environment"]["installed"], record["environment"]["digest"], record["audit"]["headline"]))
    ok = ""
    if record:
        ok = "candidate %s written to %s (promotable: %s)\n  source.tar sha256 %s\n  wheelhouse digest %s\n  environment digest %s\n  evidence digest %s" % (
            record["artifact"]["name"], args.out, record["eligibility"]["promotable"], record["source"]["archive"]["sha256"],
            record["wheelhouse"]["digest"], record["environment"]["digest"], record["audit"]["evidence"]["digest"])
    return report("PACKAGING", problems, ok)


def expectations(args):
    tree = args.expect_tree or cs.expected_tree(ROOT, args.expect_commit)
    return {"repository": args.expect_repository, "commit": args.expect_commit, "tree": tree, "runId": args.expect_run_id,
            "runAttempt": args.expect_run_attempt, "event": args.expect_event, "ref": args.expect_ref,
            "artifact": args.expect_artifact, "workflowPath": None if args.local else cd.WORKFLOW_PATH, "local": args.local}


def cmd_consume(args):
    expect = expectations(args)
    if not expect["tree"]:
        return report("CONSUMER", [{"code": "no_expected_tree", "detail": "the expected commit %s is not in this checkout, so its tree "
                                    "cannot be established independently of the candidate" % args.expect_commit}], "")
    if not args.local and not (args.expect_run_id and args.expect_run_attempt):
        return report("CONSUMER", [{"code": "no_expected_run", "detail": "a CI candidate is only accepted against an expected run and attempt"}], "")
    if args.command == "verify":
        _, problems = cs.verify(args.candidate, expect)
        return report("VERIFY", problems, "verified: the candidate is %s's (tree %s), its wheels are the lock's and its evidence re-decides clean"
                      % (args.expect_commit, expect["tree"]))
    result, problems = cs.reconstruct(args.candidate, args.work, expect, now())
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(dict(result, problems=problems), fh, indent=2, sort_keys=True)
    ok = ""
    if not problems:
        planes = result["startup"]["planes"]
        ok = ("reconstructed %s offline: %d distributions, environment digest %s (identical to the record); customer plane %s, admin plane %s; "
              "%d top-level modules imported" % (result["candidate"]["artifact"], result["environment"]["installed"], result["environment"]["digest"],
                                                 planes["customer"]["detail"]["health"], planes["admin"]["detail"]["health"],
                                                 len(planes["customer"]["detail"]["imported"])))
        summary("### Backend candidate reconstruction\n\n%s\n" % ok)
    return report("RECONSTRUCTION", problems, ok)


def _evaluation(args):
    for label, value, pattern in (("--evaluation-run", args.evaluation_run, pf._ID), ("--evaluation-attempt", args.evaluation_attempt, pf._ATTEMPT)):
        if value is not None and not pattern.match(value):
            return None, [{"code": "request_invalid", "detail": "%s must be numeric" % label}]
    if (args.evaluation_run is None) != (args.evaluation_attempt is None):
        return None, [{"code": "request_invalid", "detail": "--evaluation-run and --evaluation-attempt go together"}]
    return ({"runId": args.evaluation_run, "runAttempt": args.evaluation_attempt} if args.evaluation_run else None), []


def cmd_preflight(args):
    if getattr(args, "revision", None) is not None and not pf._SHA.match(args.revision):
        return report("PREFLIGHT", [{"code": "request_invalid", "detail": "--revision must be a full commit id"}], "")
    evaluation, problems = _evaluation(args)
    if problems:
        return report("PREFLIGHT", problems, "")
    if args.action == "facts":
        request, problems = pf.resolve_request(os.environ)
        if problems:
            return report("PREFLIGHT FACTS", problems, "")
        choice, problems = pf.fetch_facts(args.out, request, args.revision, gh=args.gh, evaluation=evaluation)
        return report("PREFLIGHT FACTS", problems, "facts read for %s (%s): CI run %s attempt %s; zips downloaded unopened"
                      % (request["target"], request["source"], (choice or {}).get("runId"), (choice or {}).get("runAttempt")))
    if evaluation is None:
        return report("PREFLIGHT", [{"code": "request_invalid", "detail": "the evaluation run and attempt are required"}], "")
    if args.action == "assess":
        # The runner's step-output files and any token leave this process's environment
        # BEFORE anything runs; only this CLI writes outputs, after the scanner has exited.
        withheld = pf.withhold_runner_authority(os.environ)
        evaluation["revision"] = args.revision
        doc = pf.assess(ROOT, args.facts, args.out, args.work, evaluation, pf.now_iso, withheld=sorted(withheld))
        a = doc.get("assessment") or {}
        for key, value in (("decision", doc["decision"]), ("deadline", doc.get("deadline") or "")):
            output(key, value, environ=withheld)
        summary("### Backend candidate preflight — NOT DEPLOYED\n\n| | |\n|---|---|\n| decision | **%s** |\n| candidate | %s |\n"
                "| certified by | CI run %s attempt %s |\n| fresh assessment | %s |\n| deadline | %s |\n| deployment authorized | false |\n"
                % (doc["decision"], (doc.get("candidate") or {}).get("artifact", {}).get("name"), (doc.get("request") or {}).get("runId"),
                   (doc.get("request") or {}).get("runAttempt"), a.get("headline", "not performed"), doc.get("deadline")), environ=withheld)
        for p in doc.get("problems") or []:
            print("  ✗ %s: %s" % (p["code"], p["detail"]), file=sys.stderr)
        for r in a.get("reasons") or []:
            print("  - [%s] %s: %s" % (r["outcome"], r["code"], r["detail"]), file=sys.stderr)
        print("preflight %s: %s; deadline %s; deploymentAuthorized false; result in %s"
              % (doc["decision"].upper(), a.get("headline", "no assessment performed"), doc.get("deadline"), args.out))
        return pf.exit_code(doc)
    expect = None
    if args.expect_preflight_id is not None or args.expect_preflight_digest is not None:
        # The assessing job's report of its own upload. Given at all, it must be whole: an
        # empty output means that report is missing, which is not the same as no hint.
        if not pf._ID.match(args.expect_preflight_id or "") or pf.listing_digest(args.expect_preflight_digest) is None:
            return report("PREFLIGHT RECEIVING CHECK", [{"code": "request_invalid", "detail": "--expect-preflight-id and "
                          "--expect-preflight-digest go together: a numeric artifact id and a sha256 digest (bare, as "
                          "upload-artifact reports it, or sha256:<hex>, as the listing does)"}], "")
        expect = {"id": args.expect_preflight_id, "digest": args.expect_preflight_digest}   # receive() reads either spelling
    admitted, problems = pf.receive(ROOT, args.facts, args.work, evaluation, pf.now_iso(), margin_minutes=args.margin_minutes,
                                    expect_preflight=expect)
    ok = ""
    if admitted:
        for key in ("commit", "candidateArtifactId", "candidateDigest", "environmentDigest", "deadlineEpoch"):
            output(key, admitted[key])
        if args.admission_out:
            # The installer's input (release/installation.py): the admitted identities, nothing
            # else. It authorizes nothing: the host re-derives every one of them from the bytes.
            from . import installation as ins
            with open(args.admission_out, "w", encoding="utf-8") as fh:
                json.dump(ins.admission_from_receive(admitted), fh, indent=2, sort_keys=True)
        ok = ("RECEIVED: the preflight result for %s (candidate artifact %s, %s) reproduces as %s under the trusted policy; usable until %s "
              "(with a %d-minute margin); deploymentAuthorized false" % (admitted["commit"], admitted["candidateArtifactId"], admitted["candidateDigest"],
                                                                        admitted["outcome"], admitted["deadline"], args.margin_minutes))
        summary("### Backend candidate preflight — received, NOT DEPLOYED\n\n```json\n%s\n```\n" % json.dumps(admitted, indent=2, sort_keys=True))
    return report("PREFLIGHT RECEIVING CHECK", problems, ok)


def _profile(args):
    from . import hostprofile as hp
    doc, digest, problems = hp.load(args.profile)
    if not problems:
        problems = hp.validate(doc, rehearsal=args.rehearsal)
    if problems:
        return None, problems
    doc["_sha256"] = digest
    return doc, []


HOST_EXIT = {"verified": 0, "unchanged": 0, "adopted": 0, "resumed": 0, "refused": 1, "verification-failed": 3, "restored": 3,
             "restoration-failed": 4}


def _host_outcome(outcome):
    for p in outcome.get("problems") or []:
        print("  ✗ %s: %s" % (p["code"], p["detail"]), file=sys.stderr)
    print(json.dumps({k: v for k, v in outcome.items() if k != "problems"}, indent=2, sort_keys=True, default=str))
    print("B3-OUTCOME: %s" % outcome["stage"])
    serving = outcome.get("release")
    if isinstance(serving, str):
        # One release on both planes (the unchanged control): attested per plane all the same,
        # so a reader never has to know which outcome spelled it which way.
        serving = {plane: serving for plane in ("customer", "admin")}
    if isinstance(serving, dict):
        for plane, rid in sorted(serving.items()):
            print("B3-SERVING: %s %s" % (plane, rid or "legacy"))
    return HOST_EXIT.get(outcome["stage"], 4)


MUTATING_HOST_ACTIONS = ("deploy", "resume", "adopt-legacy", "recover-legacy")


def _host_refused(args, label, problems):
    """A mutating action refused before it changed anything a request can reach. It still
    ATTESTS that (``B3-OUTCOME: refused``): the workflow reads the attestation, and a refusal
    that printed none reads as an unknown host state, which is not what happened."""
    code = report(label, problems, "")
    if args.action in MUTATING_HOST_ACTIONS:
        print("B3-OUTCOME: refused")
    return code


def cmd_host(args):
    from . import hostprofile as hp
    from . import installation as ins
    from . import transition as tr
    if args.action == "build":
        report_ = ins.build(args.candidate, args.release, args.work, args.expect_tree, args.base_python)
        print(json.dumps(report_, sort_keys=True))
        return 1 if report_.get("problems") else 0
    if args.action == "reconcile":
        answer = ins.reconcile_worker(args.release)
        print(json.dumps(answer, sort_keys=True))
        return 1 if answer.get("problems") else 0
    profile, problems = _profile(args)
    if problems:
        return _host_refused(args, "HOST", problems)
    if args.action == "validate-profile":
        return report("PROFILE", [], "profile %s is structurally valid (%s, %s)" % (args.profile, profile["kind"], profile["status"]))
    if args.action == "status":
        print(json.dumps(tr.status(profile), indent=2, sort_keys=True))
        return 0
    if not ins._OPERATION.match(args.operation or ""):
        return _host_refused(args, "HOST", [{"code": "operation_invalid", "detail": "--operation is lowercase words and digits"}])
    if args.action == "resume":
        return _host_outcome(tr.resume(profile, args.operation, schema_established=args.schema_established))
    if args.action == "adopt-legacy":
        return _host_outcome(tr.adopt_legacy(profile, args.operation))
    if args.action == "recover-legacy":
        return _host_outcome(tr.recover_legacy(profile, args.operation))
    # deploy: admit -> prepare -> promote. Serving is not touched until promote holds the lock.
    trusted = os.path.realpath(args.trusted)
    problems = hp.host_problems(profile)
    for path in (trusted, os.path.join(trusted, "release"), os.path.join(trusted, "dependency_audit")):
        hp._root_owned_not_writable(path, "the trusted verifier", problems)
    blocked = hp.untraversable(os.path.join(trusted, "release"))
    if blocked:
        problems.append({"code": "trusted_not_traversable", "detail": "the unprivileged identities import the trusted verifier and run "
                         "from inside it, so every directory on the way to it must grant execute to others (0711 keeps it unlistable): "
                         "%s" % ", ".join(blocked)})
    admission, err = ins.read_json(args.admission)
    if err:
        problems.append({"code": "admission_invalid", "detail": err})
    if problems:
        return _host_refused(args, "HOST DEPLOY", problems)
    work = os.path.join(profile["releaseRoot"], ".work", args.operation)
    if os.path.lexists(work):
        return _host_refused(args, "HOST DEPLOY", [{"code": "operation_reused", "detail": "%s exists: an operation id is used once" % work}])
    os.makedirs(work, mode=0o755)
    try:
        import time as _time
        state, problems = ins.admit(trusted, admission, args.candidate_zip, args.preflight_zip, os.path.join(work, "admitted"), int(_time.time()))
        if problems:
            return _host_refused(args, "HOST ADMISSION", problems)
        print("B3-ADMITTED: %s %s preflight %s until %s" % (admission["commit"], admission["candidateDigest"], admission["preflightDigest"], admission["deadline"]))
        receipt, problems = ins.prepare(profile, state, args.operation, trusted, work, rehearsal=args.rehearsal)
        if problems:
            return _host_refused(args, "HOST PREPARATION", problems)
        release = os.path.join(profile["releaseRoot"], receipt["releaseId"])
        print("B3-PREPARED: %s (%s)" % (receipt["releaseId"], "reused" if receipt.get("reused") else "installed"))
        return _host_outcome(tr.promote(profile, args.operation, release, admission, trusted, rehearsal=args.rehearsal))
    finally:
        ins._remove_attributable(work)


def main(argv):
    parser = argparse.ArgumentParser(prog="python -B -m release")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("lock")
    p.add_argument("action", choices=("check", "generate"))
    p.add_argument("--work")
    p = sub.add_parser("observe")
    p.add_argument("--out", required=True)
    p = sub.add_parser("acquire")
    p.add_argument("--wheelhouse", required=True)
    p = sub.add_parser("install")
    for flag in ("--wheelhouse", "--venv", "--work"):
        p.add_argument(flag, required=True)
    p = sub.add_parser("interpreter")
    p.add_argument("--venv", required=True)
    p = sub.add_parser("package")
    for flag in ("--wheelhouse", "--venv", "--evidence", "--before", "--out"):
        p.add_argument(flag, required=True)
    p.add_argument("--local", action="store_true")
    p.add_argument("--step-outcomes")
    for name in ("verify", "reconstruct"):
        p = sub.add_parser(name)
        p.add_argument("--candidate", required=True)
        p.add_argument("--expect-commit", required=True)
        p.add_argument("--expect-tree")
        p.add_argument("--expect-repository", default=cd.REPOSITORY)
        p.add_argument("--expect-run-id")
        p.add_argument("--expect-run-attempt")
        p.add_argument("--expect-event")
        p.add_argument("--expect-ref")
        p.add_argument("--expect-artifact")
        p.add_argument("--local", action="store_true")
        if name == "reconstruct":
            p.add_argument("--work", required=True)
            p.add_argument("--report")
    p = sub.add_parser("preflight")
    p.add_argument("action", choices=("facts", "assess", "verify"))
    p.add_argument("--facts")
    p.add_argument("--out")
    p.add_argument("--work")
    p.add_argument("--revision")
    p.add_argument("--evaluation-run")
    p.add_argument("--evaluation-attempt")
    p.add_argument("--expect-preflight-id")
    p.add_argument("--expect-preflight-digest")
    p.add_argument("--margin-minutes", type=int, default=pf.RECEIVING_MARGIN_MINUTES)
    p.add_argument("--admission-out")
    p.add_argument("--gh", default="gh")
    p = sub.add_parser("host")
    p.add_argument("action", choices=("validate-profile", "deploy", "status", "resume", "adopt-legacy", "recover-legacy", "build", "reconcile"))
    for flag in ("--profile", "--operation", "--admission", "--candidate-zip", "--preflight-zip", "--trusted",
                 "--candidate", "--release", "--work", "--expect-tree", "--base-python"):
        p.add_argument(flag)
    p.add_argument("--rehearsal", action="store_true")
    p.add_argument("--schema-established", help="resume only: an operator's statement, after inspecting the database, of the schema "
                   "state a migration left unresolved (at least 20 characters); recorded in the journal verbatim")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 64 if error.code else 0
    if args.command == "preflight":
        needed = {"facts": ("out", "revision"), "assess": ("facts", "out", "work", "revision"), "verify": ("facts", "work")}[args.action]
        if any(getattr(args, n) is None for n in needed):
            print("preflight %s needs %s" % (args.action, ", ".join("--" + n for n in needed)), file=sys.stderr)
            return 64
        if args.margin_minutes < pf.RECEIVING_MARGIN_MINUTES:
            # A receiver may demand MORE headroom before a deadline, never less.
            print("--margin-minutes can be raised above %d, never lowered" % pf.RECEIVING_MARGIN_MINUTES, file=sys.stderr)
            return 64
    if args.command == "host":
        needed = {"validate-profile": ("profile",), "status": ("profile",), "resume": ("profile", "operation"),
                  "adopt-legacy": ("profile", "operation"), "recover-legacy": ("profile", "operation"),
                  "deploy": ("profile", "operation", "admission", "candidate_zip", "preflight_zip", "trusted"),
                  "build": ("candidate", "release", "work", "expect_tree", "base_python"), "reconcile": ("release",)}[args.action]
        if any(getattr(args, n) is None for n in needed):
            print("host %s needs %s" % (args.action, ", ".join("--" + n.replace("_", "-") for n in needed)), file=sys.stderr)
            return 64
    handlers = {"host": cmd_host, "lock": cmd_lock, "observe": cmd_observe, "acquire": cmd_acquire, "install": cmd_install,
                "interpreter": cmd_interpreter, "package": cmd_package, "verify": cmd_consume, "reconstruct": cmd_consume,
                "preflight": cmd_preflight}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
