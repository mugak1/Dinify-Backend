"""THE ORCHESTRATION: snapshot the validated environment, scan THAT inventory with the
pinned scanner, prove nothing moved while it was scanned, and decide.

    snapshot  — offline. Records exactly what ``pip install -r requirements.txt`` left in
                the validation environment, right after installation. Refuses if a
                declared requirement is missing, the interpreter is not the target, or a
                package came from somewhere no index advisory can describe.
    audit     — network. Refuses to scan anything but the snapshotted inventory, creates
                the scanner's own venv from the hash-pinned requirements, proves the
                target did not move, scans the application inventory AND the scanner's
                own, re-checks the inventory after each, and writes the complete raw output
                beside the decision.
    evaluate  — offline. Re-decides retained evidence, refusing evidence recorded for a
                different revision, inventory or environment, or raw output that is not
                the bytes that were recorded.

The runner is a parameter so the regression matrix can drive every failure mode; the CLI
passes the real one and has no option that substitutes another scanner.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import core
from . import pip_adapter as pa

SNAPSHOT_SCHEMA = "dinify.dependency-audit.snapshot/v1"
COLLECTION_SCHEMA = "dinify.dependency-audit.collection/v1"
RESULT_SCHEMA = "dinify.dependency-audit.result/v1"
POLICY_SCHEMA = "dinify.dependency-audit.policy/v1"
_POLICY_KEYS = ("schema", "repository", "ecosystem", "target", "scanner", "records")
_SCANNER_KEYS = ("package", "version", "requirements", "index", "timeoutSeconds")

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_evidence(path, schema):
    """One retained evidence document, or the reason it is not one: ``(doc, None)`` or
    ``(None, problem)``.

    JSON that parses is not yet evidence. ``{}``, ``[]``, ``null``, ``0``, ``""`` and
    ``false`` all parse, and a caller that read a falsy document as "nothing to check"
    skipped every binding and graph check — so an evidence directory holding ``{}``
    re-decided to within policy, exit 0 (Codex review on mugak1/Dinify-Backend#339).
    Only a JSON object carrying the expected schema is returned; everything else comes
    back as a problem, so an absent document always has a named reason."""
    name = os.path.basename(path)
    try:
        doc = _read_json(path)
    except (OSError, ValueError) as error:
        return None, "%s: %s" % (name, error)
    if not isinstance(doc, dict):
        return None, "%s is not a JSON object" % name
    if doc.get("schema") != schema:
        return None, "%s: schema is not recognised" % name
    return doc, None


def _snapshot_problems_readable(snap):
    """A snapshot records its own problems as a list of objects; anything else is malformed."""
    problems = snap.get("problems")
    return isinstance(problems, list) and all(isinstance(p, dict) for p in problems)


def _recorded_run(g):
    """A recorded scan names its raw output file and that file's digest, or it is malformed."""
    run = g.get("run") if isinstance(g, dict) else None
    if isinstance(run, dict) and isinstance(run.get("stdoutFile"), str) and isinstance(run.get("stdoutSha256"), str):
        return run
    return None


def _recorded_packages(g):
    """The scanner inventory a collection recorded, or None when it is not one."""
    packages = g.get("packages")
    if isinstance(packages, list) and all(
            isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("version"), str)
            # None is legitimate: pip_adapter records it for a package with no metadata files.
            and (p.get("recordSha256") is None or isinstance(p.get("recordSha256"), str))
            for p in packages):
        return packages
    return None


def _write_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=False, default=sorted)
        fh.write("\n")


def _canonical(value):
    return json.dumps(value, sort_keys=True)


def load_policy(root):
    """The committed policy, validated. Returns (policy, problems).

    ``policy`` is None whenever ANY problem was recorded, not only when the file will not
    parse. Every caller guards on a truthy policy and then indexes ``target``, ``scanner``
    and ``records``, so handing back a dict that failed validation turns a named
    ``policy_invalid`` into an uncaught KeyError or TypeError — an audit that crashes
    instead of reporting itself incomplete. An invalid policy is no policy: the problems
    carry the explanation, and the outcome is ``incomplete`` (exit 2) on every path."""
    path = os.path.join(root, "dependency_audit", "policy.json")
    try:
        policy = _read_json(path)
    except (OSError, ValueError) as error:
        return None, [{"code": "policy_unreadable", "detail": "dependency_audit/policy.json: %s" % error}]
    problems = []

    def p(detail):
        problems.append({"code": "policy_invalid", "detail": detail})

    if not isinstance(policy, dict):
        return None, [{"code": "policy_invalid", "detail": "the policy is not an object"}]
    for key in policy:
        if key not in _POLICY_KEYS:
            p('unknown policy field "%s"' % key)
    if policy.get("schema") != POLICY_SCHEMA:
        p("schema is not %s" % POLICY_SCHEMA)
    if policy.get("ecosystem") != "pypi":
        p("ecosystem is not pypi")
    if not (isinstance(policy.get("repository"), str) and policy["repository"].startswith("mugak1/")):
        p("repository is not a mugak1 repository")
    target = policy.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("python"), str):
        p("target.python must name the exact validation interpreter")
    s = policy.get("scanner")
    if not isinstance(s, dict):
        p("scanner is missing")
    else:
        for key in s:
            if key not in _SCANNER_KEYS:
                p('unknown scanner field "%s"' % key)
        if s.get("package") != "pip-audit":
            p("scanner.package must be pip-audit")
        if not (isinstance(s.get("version"), str) and core._full(core._VERSION_RE, s["version"])):
            p("scanner.version must be one exact version")
        if s.get("requirements") != "dependency_audit/scanner-requirements.txt":
            p("scanner.requirements must be dependency_audit/scanner-requirements.txt")
        if s.get("index") != pa.PYPI_INDEX:
            p("scanner.index must be %s" % pa.PYPI_INDEX)
        t = s.get("timeoutSeconds")
        if not isinstance(t, int) or isinstance(t, bool) or t < 30 or t > 900:
            p("scanner.timeoutSeconds must be 30..900")
    if not isinstance(policy.get("records"), list):
        p("records must be a list (it may be empty)")
    return (None if problems else policy), problems


def spawn_runner(command, args, cwd=None, env=None, timeout=None):
    """The real process runner. Output is captured whole; nothing is piped."""
    started = time.monotonic()
    result = {"command": command, "args": list(args), "cwd": cwd, "status": None, "signal": None,
              "timedOut": False, "error": None, "stdout": "", "stderr": ""}
    try:
        proc = subprocess.run([command] + list(args), cwd=cwd, env=env, timeout=timeout,
                              capture_output=True, text=True, check=False)
        result["stdout"], result["stderr"] = proc.stdout, proc.stderr
        if proc.returncode < 0:
            result["signal"] = "signal %d" % -proc.returncode
        else:
            result["status"] = proc.returncode
    except subprocess.TimeoutExpired as error:
        result["timedOut"] = True
        result["stdout"] = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
        result["stderr"] = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
    except OSError as error:
        result["error"] = "%s: %s" % (type(error).__name__, error)
    result["durationMs"] = int((time.monotonic() - started) * 1000)
    return result


def git_revision(root):
    """Always the real git: the revision is a fact about the checkout."""
    out = {}
    for key, spec in (("commit", "HEAD"), ("tree", "HEAD^{tree}")):
        r = spawn_runner("git", ["rev-parse", spec], cwd=root, timeout=30)
        if r["status"] != 0:
            return None
        out[key] = r["stdout"].strip()
    return out


def inspect_environment(python, graph, scope_of, runner):
    """The installed inventory of the environment ``python`` belongs to, via its own pip."""
    env, _ = pa.scrubbed_environment()
    run = runner(python, ["-m", "pip", "inspect", "--local"], env=env, timeout=120)
    if run.get("status") != 0:
        return [], [{"code": "inventory_unreadable", "detail": "%s: pip inspect failed (status %s): %s" % (graph, run.get("status"), (run.get("stderr") or run.get("error") or "")[-300:])}]
    packages, problems = pa.parse_inspect(run.get("stdout"), graph)
    for pkg in packages:
        pkg["scope"] = scope_of(pkg)
    return packages, problems


def capture(root, policy, python=None, runner=spawn_runner, env_facts=None, revision=None):
    """Everything a scan is bound to. Two captures are the same inventory iff equal."""
    python = python or sys.executable
    facts = env_facts or pa.environment_facts()
    packages, problems = inspect_environment(python, "application", pa.application_scope, runner)
    req_path = os.path.join(root, "requirements.txt")
    try:
        with open(req_path, "rb") as fh:
            req_bytes = fh.read()
    except OSError as error:
        req_bytes = b""
        problems.append({"code": "no_manifest", "detail": "application: requirements.txt: %s" % error})
    problems += pa.check_declared(packages, pa.declared_requirements(req_bytes.decode("utf-8", "replace")), "application")
    if policy and facts["python"] != policy["target"]["python"]:
        problems.append({"code": "target_mismatch", "detail": "Python %s is not the validation target (Python %s); this environment is not the one CI validates" % (facts["python"], policy["target"]["python"])})
    binding = {
        "repository": policy.get("repository") if policy else None,
        "revision": revision,
        "environment": facts,
        "application": {"requirementsSha256": pa.sha256(req_bytes), "inventorySha256": pa.inventory_digest(packages), "installed": len(packages)},
    }
    return packages, problems, binding


def _binding_differences(a, b):
    if not a or not b:
        return ["no binding recorded"]
    out = []
    for key in ("repository", "revision", "environment"):
        if _canonical(a.get(key)) != _canonical(b.get(key)):
            out.append("%s %s ≠ %s" % (key, _canonical(a.get(key)), _canonical(b.get(key))))
    for key in ("requirementsSha256", "inventorySha256", "installed"):
        if (a.get("application") or {}).get(key) != (b.get("application") or {}).get(key):
            out.append("application %s %s ≠ %s" % (key, (a.get("application") or {}).get(key), (b.get("application") or {}).get(key)))
    return out


def snapshot(root, evidence_dir, now, runner=spawn_runner, python=None, env_facts=None):
    os.makedirs(evidence_dir, exist_ok=True)
    policy, problems = load_policy(root)
    packages, cap_problems, binding = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=git_revision(root))
    problems = problems + cap_problems
    doc = {"schema": SNAPSHOT_SCHEMA, "capturedAt": now, "binding": binding, "problems": problems, "packages": packages}
    _write_json(os.path.join(evidence_dir, "snapshot.json"), doc)
    return not problems, problems, doc


def _pinned_set(requirements_text):
    return {name: version for name, version in pa.declared_requirements(requirements_text).items()}


def default_install_scanner(root, policy, runner, workdir):
    """Create the scanner's venv and install it from hash-pinned wheels only. Returns
    (scanner_python, problems, summary)."""
    problems = []
    venv = os.path.join(workdir, "scanner-venv")
    env, _ = pa.scrubbed_environment()
    made = runner(sys.executable, ["-m", "venv", venv], env=env, timeout=300)
    scanner_python = os.path.join(venv, "bin", "python")
    summary = {"venv": made.get("status")}
    if made.get("status") != 0:
        problems.append({"code": "scanner_install_failed", "detail": "could not create the scanner venv: %s" % (made.get("stderr") or made.get("error") or "")[-300:]})
        return scanner_python, problems, summary
    req = os.path.join(root, policy["scanner"]["requirements"])
    inst = runner(scanner_python, ["-m", "pip", "install", "--isolated", "--no-input", "--disable-pip-version-check",
                                   "--require-hashes", "--no-deps", "--only-binary=:all:", "--index-url", policy["scanner"]["index"],
                                   "-r", req], env=env, timeout=policy["scanner"]["timeoutSeconds"])
    summary["install"] = inst.get("status")
    if inst.get("status") != 0:
        problems.append({"code": "scanner_install_failed", "detail": "the pinned scanner could not be installed (status %s%s): %s" % (inst.get("status"), ", timed out" if inst.get("timedOut") else "", (inst.get("stderr") or inst.get("error") or "")[-400:])})
    return scanner_python, problems, summary


def verify_scanner(root, policy, scanner_packages):
    """The scanner venv holds exactly the hash-pinned set, and the pinned pip-audit."""
    problems = []
    with open(os.path.join(root, policy["scanner"]["requirements"]), "r", encoding="utf-8") as fh:
        want = _pinned_set(fh.read())
    have = {p["name"]: p["version"] for p in scanner_packages}
    if want.get("pip-audit") != policy["scanner"]["version"]:
        problems.append({"code": "scanner_pin", "detail": "scanner-requirements.txt pins pip-audit %s, the policy %s" % (want.get("pip-audit"), policy["scanner"]["version"])})
    if have != want:
        extra = sorted(set(have) - set(want))
        missing = sorted(set(want) - set(have))
        moved = sorted(n for n in set(have) & set(want) if have[n] != want[n])
        problems.append({"code": "scanner_pin", "detail": "the scanner venv is not the pinned set (extra %s, missing %s, different %s)" % (extra, missing, moved)})
    return problems


def _record_run(evidence_dir, graph, run):
    stdout_file, stderr_file = "%s.scanner-stdout.txt" % graph, "%s.scanner-stderr.txt" % graph
    with open(os.path.join(evidence_dir, stdout_file), "w", encoding="utf-8") as fh:
        fh.write(run.get("stdout") or "")
    with open(os.path.join(evidence_dir, stderr_file), "w", encoding="utf-8") as fh:
        fh.write(run.get("stderr") or "")
    return {
        "argv": [run.get("command")] + list(run.get("args") or []), "status": run.get("status"), "signal": run.get("signal"),
        "timedOut": run.get("timedOut"), "error": run.get("error"), "durationMs": run.get("durationMs"),
        "stdoutFile": stdout_file, "stdoutSha256": pa.sha256(run.get("stdout") or ""), "stderrFile": stderr_file,
        "stderrSha256": pa.sha256(run.get("stderr") or ""),
    }


def audit(root, evidence_dir, now, runner=spawn_runner, install_scanner=default_install_scanner, python=None, env_facts=None):
    os.makedirs(evidence_dir, exist_ok=True)
    incomplete = []
    policy, policy_problems = load_policy(root)
    incomplete += policy_problems
    revision = git_revision(root)
    collection = {"schema": COLLECTION_SCHEMA, "startedAt": now, "graphs": {}, "scanner": None, "binding": None, "removedEnvironment": []}
    findings = []
    snap = None
    snap_path = os.path.join(evidence_dir, "snapshot.json")
    if not os.path.exists(snap_path):
        incomplete.append({"code": "no_snapshot", "detail": "no inventory snapshot was taken after installation — run `snapshot` first"})
    else:
        doc, problem = _read_evidence(snap_path, SNAPSHOT_SCHEMA)
        if problem:
            incomplete.append({"code": "snapshot_unreadable", "detail": problem})
        elif not _snapshot_problems_readable(doc):
            incomplete.append({"code": "snapshot_unreadable", "detail": "snapshot.json: problems is not a list of problems"})
        else:
            snap = doc
            for pr in snap["problems"]:
                incomplete.append({"code": "snapshot_%s" % pr.get("code"), "detail": pr.get("detail")})

    workdir = tempfile.mkdtemp(prefix="dependency-audit-")
    try:
        if policy and not incomplete:
            packages, problems, binding = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=revision)
            incomplete += problems
            collection["binding"] = binding
            for d in _binding_differences((snap or {}).get("binding"), binding):
                incomplete.append({"code": "binding_mismatch", "detail": "the inventory is not the one snapshotted after installation: %s" % d})
            scanner_python = None
            if not incomplete:
                scanner_python, problems, summary = install_scanner(root, policy, runner, workdir)
                collection["scanner"] = {"package": "pip-audit", "pinned": policy["scanner"]["version"], "install": summary}
                incomplete += problems
            if not incomplete:
                # Installing the scanner must not have moved a target package.
                _, _, after_install = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=revision)
                for d in _binding_differences(binding, after_install):
                    incomplete.append({"code": "binding_mismatch", "detail": "installing the scanner changed the target environment: %s" % d})
            scanner_packages = []
            if not incomplete:
                scanner_packages, problems = inspect_environment(scanner_python, "scanner", pa.tooling_scope, runner)
                incomplete += problems
                incomplete += verify_scanner(root, policy, scanner_packages)
            if not incomplete:
                env, removed = pa.scrubbed_environment()
                collection["removedEnvironment"] = removed
                pip_audit = os.path.join(os.path.dirname(scanner_python), "pip-audit")
                graphs = (("application", packages), ("scanner", scanner_packages))
                for graph, pkgs in graphs:
                    reqs = os.path.join(evidence_dir, "%s.inventory-requirements.txt" % graph)
                    with open(reqs, "w", encoding="utf-8") as fh:
                        fh.write(pa.pins_text(pkgs))
                    cache = tempfile.mkdtemp(prefix="cache-", dir=workdir)
                    before = pa.inventory_digest(pkgs)
                    run = runner(pip_audit, pa.scanner_args(reqs, cache, 30), env=env, timeout=policy["scanner"]["timeoutSeconds"])
                    if graph == "application":
                        now_pkgs, _, _ = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=revision)
                    else:
                        now_pkgs, _ = inspect_environment(scanner_python, "scanner", pa.tooling_scope, runner)
                    if pa.inventory_digest(now_pkgs) != before:
                        incomplete.append({"code": "inventory_changed_during_audit", "detail": "%s: the installed inventory changed while it was being scanned — the result describes nothing that still exists" % graph})
                    problems, found = pa.read_report(graph, run, pkgs)
                    incomplete += problems
                    findings += found
                    collection["graphs"][graph] = {"run": _record_run(evidence_dir, graph, run), "inventorySha256": before, "installed": len(pkgs),
                                                   "packages": [{"name": p["name"], "version": p["version"], "scope": p["scope"], "recordSha256": p["recordSha256"]} for p in pkgs]}
                _, _, after = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=git_revision(root))
                for d in _binding_differences((snap or {}).get("binding"), after):
                    incomplete.append({"code": "binding_mismatch", "detail": "after the scan: %s" % d})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    result = core.evaluate(incomplete, findings, (policy or {}).get("records") or [], now)
    collection["finishedAt"] = now
    _write_json(os.path.join(evidence_dir, "collection.json"), collection)
    doc = dict({"schema": RESULT_SCHEMA, "decidedAt": now, "headline": core.headline(result)}, **result)
    _write_json(os.path.join(evidence_dir, "result.json"), doc)
    return doc


def reevaluate(root, evidence_dir, now, runner=spawn_runner, python=None, env_facts=None):
    """Offline: decide again from retained evidence, refusing evidence not for this checkout.

    The scanner venv is gone by now, so the SCANNER graph is re-read from the inventory the
    collection recorded; the application graph is re-read from the live environment."""
    incomplete, findings = [], []
    policy, problems = load_policy(root)
    incomplete += problems
    documents = {}
    for name, schema in (("collection.json", COLLECTION_SCHEMA), ("snapshot.json", SNAPSHOT_SCHEMA)):
        documents[name], problem = _read_evidence(os.path.join(evidence_dir, name), schema)
        if problem:
            incomplete.append({"code": "evidence_unreadable", "detail": problem})
    collection, snap = documents["collection.json"], documents["snapshot.json"]
    # Every way this branch is skipped has already recorded its reason: a None document
    # (_read_evidence), or a None policy (load_policy returns one only beside its problems).
    if collection is not None and snap is not None and policy:
        packages, cap_problems, current = capture(root, policy, python=python, runner=runner, env_facts=env_facts, revision=git_revision(root))
        incomplete += cap_problems
        for d in _binding_differences(collection.get("binding"), current):
            incomplete.append({"code": "evidence_foreign", "detail": "the evidence is not for this checkout: %s" % d})
        for d in _binding_differences(snap.get("binding"), collection.get("binding")):
            incomplete.append({"code": "evidence_foreign", "detail": "the evidence's scan is not bound to its snapshot: %s" % d})
        graphs = collection.get("graphs") if isinstance(collection.get("graphs"), dict) else {}
        for graph in ("application", "scanner"):
            g = graphs.get(graph)
            if not g:
                incomplete.append({"code": "evidence_missing_graph", "detail": "%s: no scan was recorded" % graph})
                continue
            recorded = _recorded_run(g)
            if recorded is None:
                incomplete.append({"code": "evidence_unreadable", "detail": "%s: the recorded scan is malformed" % graph})
                continue
            try:
                with open(os.path.join(evidence_dir, recorded["stdoutFile"]), "r", encoding="utf-8") as fh:
                    stdout = fh.read()
            except OSError:
                incomplete.append({"code": "evidence_unreadable", "detail": "%s: raw output missing" % graph})
                continue
            if pa.sha256(stdout) != recorded["stdoutSha256"]:
                incomplete.append({"code": "evidence_tampered", "detail": "%s: the raw output is not the bytes that were recorded" % graph})
                continue
            if graph == "application":
                pkgs = packages
                if pa.inventory_digest(pkgs) != g.get("inventorySha256"):
                    incomplete.append({"code": "evidence_foreign", "detail": "application: the recorded scan was of a different inventory"})
            else:
                recorded_packages = _recorded_packages(g)
                if recorded_packages is None:
                    incomplete.append({"code": "evidence_unreadable", "detail": "scanner: the recorded inventory is malformed"})
                    continue
                pkgs = [dict(p, path="scanner:site-packages/%s" % p["name"]) for p in recorded_packages]
                if pa.inventory_digest(pkgs) != g.get("inventorySha256"):
                    incomplete.append({"code": "evidence_tampered", "detail": "scanner: the recorded inventory does not match its digest"})
            run = dict(recorded, stdout=stdout)
            problems, found = pa.read_report(graph, run, pkgs)
            incomplete += problems
            findings += found
    result = core.evaluate(incomplete, findings, (policy or {}).get("records") or [], now)
    return dict({"schema": RESULT_SCHEMA, "decidedAt": now, "headline": core.headline(result)}, **result)


def render_summary(result, markdown=False):
    lines = ["### Dependency audit\n\n**%s**\n" % result["headline"] if markdown else result["headline"]]
    groups = {}
    for f in result["findings"]:
        key = (f.get("advisory"), f.get("package"), f.get("severity"), f.get("scope"), f.get("class"), f.get("disposition"))
        groups.setdefault(key, dict(f, paths=[]))["paths"].append("%s@%s" % (f.get("path"), f.get("version")))
    if groups:
        if markdown:
            lines += ["| advisory | package | severity | scope | decision | where |", "|---|---|---|---|---|---|"]
        for g in groups.values():
            decision = ("triage required" if g["class"] == "triage" else g["class"]) if g["disposition"] == "open" else "%s (%s)" % (g["disposition"], g["coveredBy"])
            if markdown:
                lines.append("| %s | %s | %s | %s | %s | %s |" % (g["advisory"], g["package"], g["severity"], g["scope"], decision, "<br>".join(g["paths"])))
            else:
                lines.append("  %s  %s  %s  %s  → %s\n      %s" % (g["advisory"], g["package"], g["severity"], g["scope"], decision, "\n      ".join(g["paths"])))
    reasons = [r for r in result["reasons"] if r["code"] != "blocking_finding"]
    if reasons:
        lines.append("\n**Why:**\n" if markdown else "Why:")
        for r in reasons:
            lines.append("%s[%s] %s: %s" % ("- " if markdown else "  - ", r["outcome"], r["code"], r["detail"]))
    return "\n".join(lines)


def publish_summary(result):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(render_summary(result, markdown=True) + "\n")
