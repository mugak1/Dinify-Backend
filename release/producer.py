"""THE PRODUCER — certify one exact source and one exact set of package files, inside the
required CI check, and package them as ONE retained candidate.

It runs in pieces around the existing validation, which is not repeated here:

    lock check        the reviewed lock still agrees with requirements.txt (offline)
    observe           the checkout IS the commit, and nothing else exists yet (offline)
    acquire           exactly the locked files, hash-verified (network: PyPI files only)
    install           a fresh environment from those files alone (offline), reconciled
    interpreter       the `python` every later step runs is that environment's
      ... every existing guard, test suite and the dependency audit, unchanged ...
    package           after ALL of it succeeded: continuity again, reconciliation again,
                      the audit decision re-derived from its retained raw output, the
                      source exported from the commit, and the record written

A candidate exists only when every required step succeeded AND every packaging check
passed. Anything short of that removes the partial output: a half-built candidate is not
a candidate. Evidence is retained by the workflow whatever happens — that is what
``always()`` is for, and it cannot turn a failed run green.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

from . import candidate as cd
from . import environment as ev
from . import lockfile as lf
from . import sourcetree as st


def _problem(code, detail):
    return {"code": code, "detail": detail}


def read_inputs(root):
    with open(os.path.join(root, lf.LOCK_PATH), "rb") as fh:
        lock_bytes = fh.read()
    with open(os.path.join(root, lf.REQUIREMENTS_PATH), "rb") as fh:
        requirements_bytes = fh.read()
    lock, problems = lf.check(lock_bytes, requirements_bytes)
    return lock, lock_bytes, requirements_bytes, problems


def interpreter_problems(venv):
    """The RUNNING interpreter belongs to the certified environment and nothing leaks into
    its import path: no PYTHONPATH/PYTHONHOME, no user site, and every sys.path entry is
    the environment, the base standard library, or the working directory."""
    problems = []
    prefix, want = os.path.realpath(sys.prefix), os.path.realpath(venv)
    if prefix != want:
        problems.append(_problem("wrong_interpreter", "python is %s (prefix %s), not the certified environment %s" % (sys.executable, prefix, want)))
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        if os.environ.get(key):
            problems.append(_problem("wrong_interpreter", "%s is set; the certified environment must not be extended" % key))
    import site
    if site.ENABLE_USER_SITE:
        problems.append(_problem("wrong_interpreter", "the user site directory is enabled"))
    base = os.path.realpath(sys.base_prefix)
    cwd = os.path.realpath(os.getcwd())
    for entry in sys.path:
        real = os.path.realpath(entry) if entry else cwd
        if not (real == cwd or real.startswith(want + os.sep) or real.startswith(base + os.sep) or real in (want, base)):
            problems.append(_problem("wrong_interpreter", "sys.path contains %s, outside the environment and the standard library" % entry))
    return problems


def github_context(env=None):
    env = os.environ if env is None else env
    return {
        "workflowRef": env.get("GITHUB_WORKFLOW_REF"), "workflowSha": env.get("GITHUB_WORKFLOW_SHA"),
        "event": env.get("GITHUB_EVENT_NAME"), "ref": env.get("GITHUB_REF"), "sha": env.get("GITHUB_SHA"),
        "headRef": env.get("GITHUB_HEAD_REF") or None, "baseRef": env.get("GITHUB_BASE_REF") or None,
        "repository": env.get("GITHUB_REPOSITORY"), "runId": env.get("GITHUB_RUN_ID"), "runAttempt": env.get("GITHUB_RUN_ATTEMPT"),
        "runNumber": env.get("GITHUB_RUN_NUMBER"), "job": env.get("GITHUB_JOB"),
        "runner": {"os": env.get("RUNNER_OS"), "arch": env.get("RUNNER_ARCH"), "image": env.get("ImageOS"), "imageVersion": env.get("ImageVersion")},
    }


def _outcomes(step_outcomes):
    """``toJSON(steps)`` (``{id: {outcome, conclusion, ...}}``) or ``{id: outcome}``."""
    out = {}
    for key, value in (step_outcomes or {}).items():
        out[key] = value.get("outcome") if isinstance(value, dict) else value
    return out


def _evidence_problems(root, evidence_dir, lock, requirements_bytes, observation, now):
    """The retained audit evidence is complete, uncorrupted, bound to this commit and this
    environment, and re-decides to what it recorded."""
    from dependency_audit import orchestrate as oc
    problems, audit = [], {}
    try:
        present = sorted(os.listdir(evidence_dir))
    except OSError as error:
        return None, [_problem("evidence_missing", "%s: %s" % (evidence_dir, error))]
    if present != sorted(cd.EVIDENCE_FILES):
        problems.append(_problem("evidence_incomplete", "the audit evidence is not exactly the expected set (missing %s, unexpected %s)"
                                 % (sorted(set(cd.EVIDENCE_FILES) - set(present)), sorted(set(present) - set(cd.EVIDENCE_FILES)))))
        return None, problems
    docs = {}
    for name, schema in (("snapshot.json", oc.SNAPSHOT_SCHEMA), ("collection.json", oc.COLLECTION_SCHEMA), ("result.json", oc.RESULT_SCHEMA)):
        docs[name], problem = oc._read_evidence(os.path.join(evidence_dir, name), schema)
        if problem:
            problems.append(_problem("evidence_unreadable", problem))
    if problems:
        return None, problems
    snap, collection, result = docs["snapshot.json"], docs["collection.json"], docs["result.json"]
    if result.get("outcome") not in cd.ACCEPTED_AUDIT_OUTCOMES or result.get("exitCode") != 0:
        problems.append(_problem("audit_not_passed", "the dependency audit decided %s (exit %s)" % (result.get("outcome"), result.get("exitCode"))))
    redecided = oc.reevaluate(root, evidence_dir, now)
    if redecided["exitCode"] != 0 or redecided["outcome"] != result.get("outcome"):
        problems.append(_problem("audit_evidence_inconsistent", "re-deciding the retained evidence gives %s (exit %s), the record says %s: %s"
                                 % (redecided["outcome"], redecided["exitCode"], result.get("outcome"),
                                    "; ".join(r["detail"] for r in redecided["reasons"][:3]))))
    binding = snap.get("binding") or {}
    revision = binding.get("revision") or {}
    if (revision.get("commit"), revision.get("tree")) != (observation.get("commit"), observation.get("tree")):
        problems.append(_problem("evidence_foreign", "the audit snapshot was taken at %s, not %s" % (revision, observation.get("commit"))))
    if (binding.get("application") or {}).get("requirementsSha256") != lf.sha256(requirements_bytes):
        problems.append(_problem("evidence_foreign", "the audit snapshot was taken against a different requirements.txt"))
    audited = sorted((p.get("name"), p.get("version")) for p in snap.get("packages") or [])
    locked = sorted((e["name"], e["version"]) for e in lf.entries(lock))
    if audited != locked:
        problems.append(_problem("evidence_foreign", "the audited inventory is not the locked set (only audited %s, only locked %s)"
                                 % (sorted(set(audited) - set(locked)), sorted(set(locked) - set(audited)))))
    listing = [{"filename": n, "sha256": cd.file_sha256(os.path.join(evidence_dir, n)), "size": os.path.getsize(os.path.join(evidence_dir, n))}
               for n in sorted(cd.EVIDENCE_FILES)]
    with open(os.path.join(root, "dependency_audit", "policy.json"), "rb") as fh:
        policy_bytes = fh.read()
    with open(os.path.join(root, "dependency_audit", "scanner-requirements.txt"), "rb") as fh:
        scanner_bytes = fh.read()
    policy = json.loads(policy_bytes.decode("utf-8"))
    audit = {
        "policySha256": lf.sha256(policy_bytes), "scannerRequirementsSha256": lf.sha256(scanner_bytes),
        "scanner": {"package": policy["scanner"]["package"], "version": policy["scanner"]["version"]},
        "outcome": result.get("outcome"), "exitCode": result.get("exitCode"), "headline": result.get("headline"),
        "counts": result.get("counts"), "snapshotCapturedAt": snap.get("capturedAt"),
        "collectionStartedAt": collection.get("startedAt"), "collectionFinishedAt": collection.get("finishedAt"),
        "decidedAt": result.get("decidedAt"), "inventorySha256": (binding.get("application") or {}).get("inventorySha256"),
        "reevaluated": {"at": now, "outcome": redecided["outcome"], "exitCode": redecided["exitCode"]},
        "evidence": {"files": listing, "digest": cd.listing_digest(listing)},
        "note": "the collection records one timestamp for its start and finish (the audit command's start); it is reproduced, not rewritten",
    }
    return audit, problems


def package(root, wheelhouse, venv, evidence_dir, before_path, out, now, step_outcomes, context=None, local=False):
    """Build the candidate at ``out``. Returns ``(record, problems)``; on any problem the
    partial output is removed and ``record`` is None."""
    problems = []
    if os.path.lexists(out):
        return None, [_problem("output_exists", "%s already exists; a candidate is only ever written fresh" % out)]
    lock, lock_bytes, requirements_bytes, lock_problems = read_inputs(root)
    if lock_problems:
        return None, lock_problems
    context = context if context is not None else (None if local else github_context())
    expected_commit = None if local else (context or {}).get("sha")
    if not local and not expected_commit:
        return None, [_problem("no_run_context", "no GitHub Actions run context: a CI candidate needs GITHUB_SHA, run and attempt")]

    problems += interpreter_problems(venv)
    outcomes = _outcomes(step_outcomes)
    for step in cd.REQUIRED_STEPS:
        if outcomes.get(step) != "success":
            problems.append(_problem("required_step_not_passed", "required step %r is %r, not success" % (step, outcomes.get(step))))
    try:
        with open(before_path, "r", encoding="utf-8") as fh:
            before = json.load(fh)
    except (OSError, ValueError) as error:
        before = None
        problems.append(_problem("continuity_unknown", "no readable before-validation observation: %s" % error))
    after = st.observe(root, st.AFTER_VALIDATION, expected_commit=expected_commit, now=now)
    problems += after["problems"]
    if before is not None:
        if before.get("phase") != st.BEFORE_VALIDATION or before.get("problems") or before.get("approvedUntracked"):
            problems.append(_problem("continuity_broken", "the before-validation observation was not a clean checkout"))
        for key in ("commit", "tree", "trackedFiles"):
            if before.get(key) != after.get(key):
                problems.append(_problem("continuity_broken", "%s changed during validation: %s -> %s" % (key, before.get(key), after.get(key))))
    if problems:
        return None, problems

    listing, wh_problems = ev.verify_wheelhouse(lock, wheelhouse)
    pins, _ = lf.parse_direct_inputs(requirements_bytes.decode("utf-8"))
    inventory, env_problems = ev.reconcile(lock, wheelhouse, venv, pins)
    problems += wh_problems + env_problems
    if inventory is not None:
        problems += ev.target_problems(lock, inventory["facts"])
    audit, audit_problems = _evidence_problems(root, evidence_dir, lock, requirements_bytes, after, now)
    problems += audit_problems
    if problems:
        return None, problems

    promotable, reason = (False, "produced outside GitHub Actions; never promotable") if local else \
        cd.eligibility(context.get("event"), context.get("ref"), context.get("repository"))
    name = cd.artifact_name(promotable, (context or {}).get("runId"), (context or {}).get("runAttempt"), local=local)
    try:
        os.makedirs(out)
        size = st.export_archive(root, after["commit"], os.path.join(out, cd.SOURCE))
        files, tar_problems = st.read_archive(os.path.join(out, cd.SOURCE))
        problems += tar_problems
        if not tar_problems and st.archive_tree(files) != after["tree"]:
            problems.append(_problem("archive_mismatch", "the exported archive does not hash to the commit's tree"))
        for path, (_, data) in sorted(files.items()):
            problems += st.forbidden_problems(path, data)
        shutil.copytree(wheelhouse, os.path.join(out, cd.WHEELHOUSE))
        copied, copy_problems = ev.verify_wheelhouse(lock, os.path.join(out, cd.WHEELHOUSE))
        problems += copy_problems
        os.makedirs(os.path.join(out, cd.EVIDENCE))
        for filename in cd.EVIDENCE_FILES:
            shutil.copyfile(os.path.join(evidence_dir, filename), os.path.join(out, cd.EVIDENCE, filename))
        evidence_listing = [{"filename": n, "sha256": cd.file_sha256(os.path.join(out, cd.EVIDENCE, n)),
                             "size": os.path.getsize(os.path.join(out, cd.EVIDENCE, n))} for n in sorted(cd.EVIDENCE_FILES)]
        if cd.listing_digest(evidence_listing) != audit["evidence"]["digest"]:
            problems.append(_problem("evidence_changed", "the evidence changed while it was being packaged"))
        final = st.observe(root, st.AFTER_VALIDATION, expected_commit=expected_commit, now=now)
        problems += final["problems"]
        if (final.get("commit"), final.get("tree")) != (after["commit"], after["tree"]):
            problems.append(_problem("continuity_broken", "the checkout moved while the candidate was being packaged"))
        if problems:
            raise RuntimeError("refused")
        record = {
            "schema": cd.RECORD_SCHEMA,
            "repository": cd.REPOSITORY,
            "commit": after["commit"],
            "tree": after["tree"],
            "createdAt": now,
            "eligibility": {"promotable": promotable, "reason": reason},
            "artifact": {"name": name, "note": "the upload's artifact ID and digest are established later from the run's artifact listing"},
            "ci": context,
            "target": {k: inventory["facts"][k] for k in ("python", "implementation", "platform", "machine", "libc", "soabi", "sysconfigPlatform")},
            "installer": {"bootstrap": {k: lock["bootstrap"][0][k] for k in ("name", "version", "filename", "sha256")},
                          "environment": "python -m venv --without-pip; the pinned installer installs itself from its wheel",
                          "flags": ev.pip_install_args("<wheelhouse>", "<requirements>")},
            "inputs": {"requirements": {"path": lf.REQUIREMENTS_PATH, "sha256": lf.sha256(requirements_bytes)},
                       "lock": {"path": lf.LOCK_PATH, "sha256": lf.sha256(lock_bytes), "schema": lock["schema"], "generator": lock["generator"]}},
            "source": {"archive": {"path": cd.SOURCE, "sha256": cd.file_sha256(os.path.join(out, cd.SOURCE)), "size": size},
                       "tree": after["tree"], "files": len(files), "contentSha256": st.listing_digest(files),
                       "rules": "every file of the commit's tree, exported by git archive; environment files, databases, keys, "
                                "dependency and generated-output paths and recognisable key material are refused, not filtered"},
            "wheelhouse": {"files": copied, "digest": cd.listing_digest(copied)},
            "environment": {"packages": inventory["packages"], "digest": inventory["digest"], "installed": inventory["installed"],
                            "pipCheck": inventory["pipCheck"], "markers": inventory["markers"],
                            "auditInventorySha256": audit["inventorySha256"]},
            "validation": {"requiredSteps": {s: outcomes[s] for s in cd.REQUIRED_STEPS}},
            "audit": audit,
            "continuity": {"beforeValidation": _summary(before), "afterValidation": _summary(after), "afterPackaging": _summary(final)},
        }
        with open(os.path.join(out, cd.RECORD), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return record, []
    except Exception as error:  # a refused or failed build leaves nothing behind
        shutil.rmtree(out, ignore_errors=True)
        if not problems:
            problems.append(_problem("packaging_failed", "%s: %s" % (type(error).__name__, error)))
        return None, problems


def _summary(observation):
    if not observation:
        return None
    return {k: observation.get(k) for k in ("phase", "observedAt", "commit", "tree", "trackedFiles", "approvedUntracked")}
