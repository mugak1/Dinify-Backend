"""THE CONSUMER — receive a candidate as DATA, prove it is the candidate that was expected,
and only then reconstruct its Python application environment somewhere else, offline.

Non-privileged by design: it holds no deployment credential, contacts no service, reads
no environment file and changes nothing it was given. It runs from ITS OWN checkout of the
expected commit; nothing inside the candidate is executed until it has been proven to be
that commit's bytes.

THE ORDER IS THE CONTRACT.
  1. The container: exactly record.json, source.tar, wheelhouse/ and evidence/ — regular
     files only, nothing else, no links.
  2. The record, as data: schema, and every identity compared with what the CONSUMER
     expected (repository, commit, tree, run, attempt, artifact name, eligibility) — never
     with what the record says about itself.
  3. The source archive: safe members only, and its recomputed git tree id must be the
     EXPECTED tree (from the consumer's own git, not from the record). A self-consistent
     replacement cannot pass.
  4. The lock and requirements.txt, read from that verified source; the wheelhouse must be
     exactly the lock's files, byte for byte.
  5. The retained audit evidence: complete, uncorrupted, bound to the expected commit and
     to the locked inventory, and re-decided from its raw scanner output.
  Only now is anything executed:
  6. A fresh environment built from the admitted wheels alone — no index, no cache, no
     configuration, a dead proxy so an accidental network attempt fails loudly — then
     reconciled against the record's portable inventory.
  7. Bounded startup of both planes with disposable settings (release/startup.py).

A RECONSTRUCTION IS NOT A RECERTIFICATION. It collects no advisories, re-runs no suite and
rewrites nothing: the record's timestamps remain the certification's own, and the
reconstruction report carries its own time beside them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import candidate as cd
from . import environment as ev
from . import lockfile as lf
from . import sourcetree as st

STARTUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup.py")
RECONSTRUCTION_SCHEMA = "dinify.backend.reconstruction/1"
_RECORD_KEYS = ("schema", "repository", "commit", "tree", "createdAt", "eligibility", "artifact", "ci", "target", "installer",
                "inputs", "source", "wheelhouse", "environment", "validation", "audit", "continuity")


def _problem(code, detail):
    return {"code": code, "detail": detail}


def expected_tree(repo, commit):
    """The tree id of ``commit`` from the CONSUMER's own git (never from the candidate)."""
    try:
        return st.git(repo, "rev-parse", "%s^{tree}" % commit).strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None


def _container_problems(root):
    problems = []
    if os.path.islink(root) or not os.path.isdir(root):
        return [_problem("container_invalid", "%s is not a directory" % root)]
    allowed_dirs = {cd.WHEELHOUSE, cd.EVIDENCE}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for d in list(dirnames):
            rel = d if rel_dir == "." else os.path.join(rel_dir, d)
            if os.path.islink(os.path.join(dirpath, d)) or rel not in allowed_dirs:
                problems.append(_problem("container_unexpected", "%s is not part of a candidate" % rel))
                dirnames.remove(d)
        for f in filenames:
            rel = f if rel_dir == "." else os.path.join(rel_dir, f)
            full = os.path.join(dirpath, f)
            if os.path.islink(full) or not os.path.isfile(full):
                problems.append(_problem("container_unexpected", "%s is a link or special file" % rel))
            elif rel_dir == "." and f not in (cd.RECORD, cd.SOURCE):
                problems.append(_problem("container_unexpected", "%s is not part of a candidate" % rel))
    for required in (cd.RECORD, cd.SOURCE, cd.WHEELHOUSE, cd.EVIDENCE):
        if not os.path.exists(os.path.join(root, required)):
            problems.append(_problem("container_incomplete", "%s is missing" % required))
    return problems


def _identity_problems(record, expect):
    problems = []
    ci = record.get("ci") or {}
    pairs = [("repository", record.get("repository"), expect["repository"]), ("commit", record.get("commit"), expect["commit"]),
             ("tree", record.get("tree"), expect["tree"])]
    if expect.get("local"):
        if record.get("ci") is not None or (record.get("eligibility") or {}).get("promotable") is not False:
            problems.append(_problem("identity_mismatch", "a local candidate was expected; this record claims a CI run"))
    else:
        pairs += [("run", ci.get("runId"), expect["runId"]), ("attempt", ci.get("runAttempt"), expect["runAttempt"]),
                  ("commit named by the run", ci.get("sha"), expect["commit"]),
                  ("repository named by the run", ci.get("repository"), expect["repository"])]
        if expect.get("event") is not None:
            pairs += [("event", ci.get("event"), expect["event"]), ("ref", ci.get("ref"), expect["ref"])]
        if expect.get("workflowPath"):
            workflow = (ci.get("workflowRef") or "").split("@")[0]
            pairs.append(("workflow", workflow, "%s/%s" % (expect["repository"], expect["workflowPath"])))
        promotable, _ = cd.eligibility(ci.get("event"), ci.get("ref"), ci.get("repository"))
        if (record.get("eligibility") or {}).get("promotable") is not promotable:
            problems.append(_problem("eligibility_mismatch", "the record claims promotable=%s; a %s on %s is %s"
                                     % ((record.get("eligibility") or {}).get("promotable"), ci.get("event"), ci.get("ref"), promotable)))
        name = cd.artifact_name(promotable, ci.get("runId"), ci.get("runAttempt"))
        pairs.append(("artifact name", (record.get("artifact") or {}).get("name"), name))
        if expect.get("artifact"):
            pairs.append(("received artifact", expect["artifact"], name))
    for label, actual, wanted in pairs:
        if actual != wanted:
            problems.append(_problem("identity_mismatch", "%s is %r; expected %r" % (label, actual, wanted)))
    return problems


def _evidence_state(candidate, files, record, lock, requirements_bytes, expect):
    """Re-derive the audit decision from the retained raw output, and bind it."""
    from dependency_audit import core, orchestrate as oc, pip_adapter as pa
    problems = []
    evidence = os.path.join(candidate, cd.EVIDENCE)
    present = sorted(os.listdir(evidence))
    if present != sorted(cd.EVIDENCE_FILES):
        return [_problem("evidence_incomplete", "the evidence is not exactly the expected set (missing %s, unexpected %s)"
                         % (sorted(set(cd.EVIDENCE_FILES) - set(present)), sorted(set(present) - set(cd.EVIDENCE_FILES))))]
    listing = [{"filename": n, "sha256": cd.file_sha256(os.path.join(evidence, n)), "size": os.path.getsize(os.path.join(evidence, n))} for n in present]
    audit = record.get("audit") or {}
    if listing != (audit.get("evidence") or {}).get("files") or cd.listing_digest(listing) != (audit.get("evidence") or {}).get("digest"):
        problems.append(_problem("evidence_mismatch", "the evidence files are not the ones the record lists"))
    docs, unreadable = {}, []
    for name, schema in (("snapshot.json", oc.SNAPSHOT_SCHEMA), ("collection.json", oc.COLLECTION_SCHEMA), ("result.json", oc.RESULT_SCHEMA)):
        docs[name], problem = oc._read_evidence(os.path.join(evidence, name), schema)
        if problem:
            unreadable.append(_problem("evidence_unreadable", problem))
    if unreadable:
        return problems + unreadable
    snap, collection, result = docs["snapshot.json"], docs["collection.json"], docs["result.json"]
    binding = snap.get("binding") or {}
    if (binding.get("revision") or {}) != {"commit": expect["commit"], "tree": expect["tree"]}:
        problems.append(_problem("evidence_foreign", "the audit snapshot names %s, not the expected commit" % binding.get("revision")))
    if cd.canonical(collection.get("binding")) != cd.canonical(binding):
        problems.append(_problem("evidence_foreign", "the scan is not bound to the snapshot"))
    if (binding.get("application") or {}).get("requirementsSha256") != lf.sha256(requirements_bytes):
        problems.append(_problem("evidence_foreign", "the snapshot was taken against a different requirements.txt"))
    if (binding.get("application") or {}).get("inventorySha256") != (record.get("environment") or {}).get("auditInventorySha256"):
        problems.append(_problem("evidence_foreign", "the audited inventory is not the certified environment's"))
    audited = sorted((p.get("name"), p.get("version")) for p in snap.get("packages") or [] if isinstance(p, dict))
    if audited != sorted((e["name"], e["version"]) for e in lf.entries(lock)):
        problems.append(_problem("evidence_foreign", "the audited inventory is not the locked set"))
    try:
        policy = json.loads(files["dependency_audit/policy.json"][1].decode("utf-8"))
    except (KeyError, ValueError) as error:
        return problems + [_problem("evidence_unreadable", "the verified source has no readable audit policy: %s" % error)]
    if lf.sha256(files["dependency_audit/policy.json"][1]) != audit.get("policySha256"):
        problems.append(_problem("evidence_mismatch", "the record names a different audit policy"))
    incomplete, findings = [], []
    graphs = collection.get("graphs") if isinstance(collection.get("graphs"), dict) else {}
    for graph in ("application", "scanner"):
        g = graphs.get(graph) or {}
        recorded = oc._recorded_run(g)
        if recorded is None:
            problems.append(_problem("evidence_unreadable", "%s: no recorded scan" % graph))
            continue
        path = os.path.join(evidence, recorded["stdoutFile"]) if recorded["stdoutFile"] in cd.EVIDENCE_FILES else None
        if path is None:
            problems.append(_problem("evidence_unreadable", "%s: the recorded output file is not an evidence file" % graph))
            continue
        with open(path, "r", encoding="utf-8") as fh:
            stdout = fh.read()
        if pa.sha256(stdout) != recorded["stdoutSha256"]:
            problems.append(_problem("evidence_tampered", "%s: the raw scanner output is not the bytes that were recorded" % graph))
            continue
        if graph == "application":
            pkgs = [dict(p) for p in snap.get("packages") or []]
        else:
            listed = oc._recorded_packages(g)
            if listed is None:
                problems.append(_problem("evidence_unreadable", "scanner: the recorded inventory is malformed"))
                continue
            pkgs = [dict(p, path="scanner:site-packages/%s" % p["name"]) for p in listed]
        if pa.inventory_digest(pkgs) != g.get("inventorySha256"):
            problems.append(_problem("evidence_tampered", "%s: the recorded inventory does not match its digest" % graph))
        found_problems, found = pa.read_report(graph, dict(recorded, stdout=stdout), pkgs)
        incomplete += found_problems
        findings += found
    decision = core.evaluate(incomplete, findings, policy.get("records") or [], result.get("decidedAt"))
    if decision["outcome"] != result.get("outcome") or decision["exitCode"] != result.get("exitCode"):
        problems.append(_problem("evidence_inconsistent", "the raw evidence re-decides to %s (exit %s); result.json says %s (exit %s)"
                                 % (decision["outcome"], decision["exitCode"], result.get("outcome"), result.get("exitCode"))))
    if decision["outcome"] not in cd.ACCEPTED_AUDIT_OUTCOMES or decision["exitCode"] != 0:
        problems.append(_problem("audit_not_passed", "the retained audit decides %s: no eligible candidate carries that" % decision["outcome"]))
    if result.get("outcome") != audit.get("outcome"):
        problems.append(_problem("evidence_mismatch", "the record and result.json disagree about the audit outcome"))
    return problems


def verify(candidate, expect):
    """Everything short of execution. Returns ``(state, problems)``."""
    problems = _container_problems(candidate)
    if problems:
        return None, problems
    try:
        with open(os.path.join(candidate, cd.RECORD), "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError) as error:
        return None, [_problem("record_unreadable", str(error))]
    if not isinstance(record, dict) or record.get("schema") != cd.RECORD_SCHEMA:
        return None, [_problem("record_unsupported", "the record is not a %s document" % cd.RECORD_SCHEMA)]
    missing = [k for k in _RECORD_KEYS if k not in record]
    unknown = [k for k in record if k not in _RECORD_KEYS]
    if missing or unknown:
        return None, [_problem("record_invalid", "the record is missing %s and carries unknown %s" % (missing, unknown))]
    problems += _identity_problems(record, expect)
    if problems:
        return None, problems

    source = os.path.join(candidate, cd.SOURCE)
    archive = (record.get("source") or {}).get("archive") or {}
    if (cd.file_sha256(source), os.path.getsize(source)) != (archive.get("sha256"), archive.get("size")):
        problems.append(_problem("source_mismatch", "source.tar is not the archive the record describes"))
    files, tar_problems = st.read_archive(source)
    problems += tar_problems
    if tar_problems:
        return None, problems
    if st.archive_tree(files) != expect["tree"]:
        return None, problems + [_problem("source_mismatch", "source.tar does not hash to the expected tree %s" % expect["tree"])]
    if st.listing_digest(files) != (record.get("source") or {}).get("contentSha256"):
        problems.append(_problem("source_mismatch", "the source content digest differs from the record"))
    for path, (_, data) in sorted(files.items()):
        problems += st.forbidden_problems(path, data)
    try:
        lock_bytes, requirements_bytes = files[lf.LOCK_PATH][1], files[lf.REQUIREMENTS_PATH][1]
    except KeyError as error:
        return None, problems + [_problem("source_incomplete", "the verified source has no %s" % error)]
    lock, lock_problems = lf.check(lock_bytes, requirements_bytes)
    problems += lock_problems
    inputs = record.get("inputs") or {}
    if lf.sha256(lock_bytes) != (inputs.get("lock") or {}).get("sha256") or \
            lf.sha256(requirements_bytes) != (inputs.get("requirements") or {}).get("sha256"):
        problems.append(_problem("inputs_mismatch", "the record names a different lock or requirements.txt than the verified source holds"))
    if lock is None:
        return None, problems
    listing, wh_problems = ev.verify_wheelhouse(lock, os.path.join(candidate, cd.WHEELHOUSE))
    problems += wh_problems
    wheelhouse = record.get("wheelhouse") or {}
    if not wh_problems and (listing != wheelhouse.get("files") or cd.listing_digest(listing) != wheelhouse.get("digest")):
        problems.append(_problem("wheelhouse_mismatch", "the wheelhouse is not the one the record lists"))
    recorded_packages = (record.get("environment") or {}).get("packages") or []
    if sorted((p.get("name"), p.get("version"), p.get("filename"), p.get("wheelSha256")) for p in recorded_packages) != \
            sorted((e["name"], e["version"], e["filename"], e["sha256"]) for e in lf.entries(lock)):
        problems.append(_problem("environment_mismatch", "the record's certified environment is not the locked set"))
    problems += _evidence_state(candidate, files, record, lock, requirements_bytes, expect)
    if problems:
        return None, problems
    return {"record": record, "files": files, "lock": lock, "requirements": requirements_bytes}, []


def env_files_above(path):
    """Environment files on the way from ``path`` to the filesystem root — reported by
    name only, never opened, because python-decouple searches upward for them."""
    found, current = [], os.path.realpath(path)
    while True:
        for name in (".env", "settings.ini"):
            if os.path.lexists(os.path.join(current, name)):
                found.append(os.path.join(current, name))
        parent = os.path.dirname(current)
        if parent == current:
            return found
        current = parent


def top_level_imports(lock, wheelhouse):
    """Importable top-level names each installed wheel provides into site-packages."""
    names = set()
    for entry in lf.entries(lock):
        manifest, _, _ = ev.wheel_manifest(os.path.join(wheelhouse, entry["filename"]))
        for rel in manifest:
            head = rel.split("/")[0]
            if head.endswith((".dist-info", ".data")) or head == "__pycache__":
                continue
            if "/" in rel and rel == head + "/__init__.py":
                names.add(head)
            elif "/" not in rel and rel.endswith(".py"):
                names.add(rel[:-3])
            elif "/" not in rel and rel.endswith(".so"):
                names.add(rel.split(".")[0])
    return sorted(names)


def reconstruct(candidate, work, expect, now, base_python=None, startup=STARTUP):
    """Verify, then rebuild and start. Returns ``(report, problems)``. ``startup`` is the
    bounded-startup script (always the consumer's own; tests substitute a stand-in for
    a synthetic application)."""
    state, problems = verify(candidate, expect)
    report = {"schema": RECONSTRUCTION_SCHEMA, "reconstructedAt": now, "expected": expect,
              "notice": "a reconstruction proves the retained inputs rebuild this environment offline and start the "
                        "application within the declared target; it collects no advisories and recertifies nothing"}
    if problems:
        return dict(report, verified=False), problems
    record, lock = state["record"], state["lock"]
    report.update(verified=True, candidate={"commit": record["commit"], "tree": record["tree"], "artifact": record["artifact"]["name"],
                                            "run": (record.get("ci") or {}).get("runId"), "attempt": (record.get("ci") or {}).get("runAttempt"),
                                            "createdAt": record["createdAt"], "recordSha256": cd.file_sha256(os.path.join(candidate, cd.RECORD))})
    if os.path.lexists(work) and os.listdir(work):
        return report, [_problem("workdir_not_empty", "%s must be empty" % work)]
    source, venv = os.path.join(work, "source"), os.path.join(work, "venv")
    os.makedirs(work, exist_ok=True)
    found = env_files_above(work)
    if found:
        return report, [_problem("environment_file_nearby", "an environment file exists above the reconstruction directory (%s); "
                                 "the application would search upward and could read it, so nothing is started" % ", ".join(found))]
    st.extract(state["files"], source)
    wheelhouse = os.path.join(candidate, cd.WHEELHOUSE)
    install, problems = ev.create_environment(lock, wheelhouse, venv, os.path.join(work, "install"), base_python=base_python)
    report["install"] = {"steps": [{k: s[k] for k in ("name", "status", "seconds")} for s in install.get("steps", [])],
                         "pipInputs": install.get("pipInputs")}
    if problems:
        return report, problems
    pins, _ = lf.parse_direct_inputs(state["requirements"].decode("utf-8"))
    inventory, problems = ev.reconcile(lock, wheelhouse, venv, pins)
    if inventory is None:
        return report, problems
    problems += ev.target_problems(lock, inventory["facts"])
    environment = record["environment"]
    if inventory["packages"] != environment.get("packages") or inventory["digest"] != environment.get("digest"):
        problems.append(_problem("environment_mismatch", "the reconstructed environment is not the certified one (digest %s, record %s)"
                                 % (inventory["digest"], environment.get("digest"))))
    report["environment"] = {"digest": inventory["digest"], "installed": inventory["installed"], "pipCheck": inventory["pipCheck"],
                             "matchesRecord": inventory["digest"] == environment.get("digest")}
    report["target"] = {"reconstructed": {k: inventory["facts"][k] for k in ("python", "implementation", "platform", "machine", "libc")},
                        "certified": {k: record["target"].get(k) for k in ("python", "implementation", "platform", "machine", "libc")}}
    for key in ("python", "implementation", "platform", "machine"):
        if inventory["facts"][key] != record["target"].get(key):
            problems.append(_problem("target_mismatch", "%s is %r here, %r where it was certified" % (key, inventory["facts"][key], record["target"].get(key))))
    if problems:
        return report, problems
    imports = top_level_imports(lock, wheelhouse)
    scratch = os.path.join(work, "startup")
    os.makedirs(os.path.join(scratch, "home"))
    step = ev.run([ev.venv_python(venv), "-I", startup, "--source", source, "--scratch", scratch, "--imports", ",".join(imports)],
                  cwd=scratch, env=ev.scrubbed_env(dict(ev.OFFLINE, HOME=os.path.join(scratch, "home"))), timeout=600)
    try:
        answer = json.loads(step["stdout"]) if step["status"] is not None else None
    except ValueError:
        answer = None
    report["startup"] = answer if answer is not None else {"status": step["status"], "stderr": step["stderr"][-1500:]}
    if step["status"] != 0 or not answer or not answer.get("ok"):
        problems.append(_problem("startup_failed", "the application did not start from the reconstructed environment: %s"
                                 % (json.dumps((answer or {}).get("failures")) if answer else step["stderr"][-800:])))
    return report, problems


def discard(path):
    shutil.rmtree(path, ignore_errors=True)
