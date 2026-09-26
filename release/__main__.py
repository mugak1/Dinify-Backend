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

Exit status: 0 success · 1 refused (the reasons are printed) · 64 usage. Run every command
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


def summary(markdown):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def output(key, value):
    target = os.environ.get("GITHUB_OUTPUT")
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
    outcomes = None
    if args.step_outcomes:
        with open(args.step_outcomes, "r", encoding="utf-8") as fh:
            outcomes = json.load(fh)
    elif os.environ.get("STEP_OUTCOMES"):
        outcomes = json.loads(os.environ["STEP_OUTCOMES"])
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
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 64 if error.code else 0
    handlers = {"lock": cmd_lock, "observe": cmd_observe, "acquire": cmd_acquire, "install": cmd_install,
                "interpreter": cmd_interpreter, "package": cmd_package, "verify": cmd_consume, "reconstruct": cmd_consume}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
