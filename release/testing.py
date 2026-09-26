"""Test support for ``release/tests_*.py`` — never imported by the producer or the consumer.

Everything here is REAL except the packages: synthetic wheels built to the wheel
specification, a disposable git repository laid out like this one (carrying copies of the
real ``release`` and ``dependency_audit`` code, so the real CLIs run against it), real
virtual environments, and dependency-audit evidence written in the real formats and read
back through the real reader. The bootstrap installer is the standard library's own
bundled pip wheel (``ensurepip/_bundled``), so the whole chain — bootstrap, offline
install, reconciliation, packaging, reconstruction — runs with no network.

The real lock bootstraps pip 26.2.1; these fixtures bootstrap the interpreter's bundled pip
(24.0 on CPython 3.12.3). The mechanism is the same; the version is not the point.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile

from . import lockfile as lf

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
NOW = "2026-09-26T12:00:00.000Z"
GIT = ["git", "-c", "user.name=release-tests", "-c", "user.email=release-tests@invalid", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def record_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def make_wheel(directory, name, version, requires=(), tag="py3-none-any", modules=None, after_record=None):
    """Write a spec-conformant wheel; ``after_record`` may alter members AFTER the RECORD
    was computed (a wheel whose bytes no longer match its own RECORD)."""
    dist = lf.normalize(name).replace("-", "_")
    files = dict(modules or {"%s/__init__.py" % dist: "VERSION = %r\n" % version})
    info = "%s-%s.dist-info" % (dist, version)
    files[info + "/METADATA"] = "Metadata-Version: 2.1\nName: %s\nVersion: %s\n%s" % (
        name, version, "".join("Requires-Dist: %s\n" % r for r in requires))
    files[info + "/WHEEL"] = "Wheel-Version: 1.0\nGenerator: dinify-release-tests\nRoot-Is-Purelib: true\nTag: %s\n" % tag
    rows = [(p, record_hash(d.encode("utf-8")), str(len(d.encode("utf-8")))) for p, d in files.items()]
    rows.append((info + "/RECORD", "", ""))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    files[info + "/RECORD"] = buf.getvalue()
    if after_record:
        files = after_record(dict(files))
    path = os.path.join(directory, "%s-%s-%s.whl" % (dist, version, tag))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, d in files.items():
            zf.writestr(p, d)
    return path


def bundled_pip():
    import ensurepip
    bundled = os.path.join(os.path.dirname(ensurepip.__file__), "_bundled")
    wheels = sorted(f for f in os.listdir(bundled) if f.startswith("pip-") and f.endswith(".whl"))
    if not wheels:
        raise RuntimeError("this interpreter's ensurepip carries no bundled pip wheel; the release tests need it")
    return os.path.join(bundled, wheels[-1])


def target():
    impl = sys.implementation.version
    version = "%d.%d.%d" % (impl.major, impl.minor, impl.micro)
    markers = {
        "implementation_name": sys.implementation.name, "implementation_version": version, "os_name": os.name,
        "platform_machine": platform.machine(), "platform_python_implementation": platform.python_implementation(),
        "platform_system": platform.system(), "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]), "sys_platform": sys.platform,
    }
    return {"python": platform.python_version(), "implementation": platform.python_implementation(), "platform": sys.platform,
            "machine": platform.machine(), "glibc": platform.libc_ver()[1], "markers": markers}


def entry(path, direct=None, required_by=()):
    filename = os.path.basename(path)
    parsed = lf.parse_wheel_filename(filename)
    with open(path, "rb") as fh:
        data = fh.read()
    e = {"name": parsed["name"], "version": parsed["version"], "filename": filename,
         "url": lf.FILE_HOST + "fixtures/" + filename, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    if direct is not None:
        e.update(direct=direct, requiredBy=sorted(required_by))
    return e


def make_lock(requirements_bytes, bootstrap_path, packages):
    """``packages``: [(wheel path, direct, requiredBy)]."""
    boot = entry(bootstrap_path)
    return {
        "schema": lf.LOCK_SCHEMA, "repository": lf.REPOSITORY, "target": target(),
        "directInputs": {"path": lf.REQUIREMENTS_PATH, "sha256": lf.sha256(requirements_bytes)},
        "generator": {"tool": "pip", "version": boot["version"], "wheelSha256": boot["sha256"], "index": lf.INDEX,
                      "invocation": ["pip", "install", "--dry-run", "--report", "<report>", "-r", "requirements.txt"],
                      "resolvedAt": "2026-09-26T12:00:00Z", "reportSha256": lf.sha256("synthetic resolution")},
        "bootstrap": [boot],
        "packages": sorted((entry(p, d, r) for p, d, r in packages), key=lambda e: e["name"]),
    }


def run(argv, cwd=None, env=None, timeout=600):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def git(root, *args):
    proc = run(GIT + ["-C", root] + list(args))
    if proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def write(root, path, data, mode=None):
    full = os.path.join(root, *path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mode:
        os.chmod(full, mode)
    return full


def init_repo(root, files):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    for path, data in files.items():
        write(root, path, data)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}")


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PIP_", "PYTHON", "GITHUB_", "RUNNER_"))
           and k not in ("VIRTUAL_ENV", "STEP_OUTCOMES", "DJANGO_SETTINGS_MODULE")}
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra or {})
    return env


def base_python():
    return getattr(sys, "_base_executable", None) or sys.executable


# --- a synthetic project with this repository's layout --------------------------------

APP = "demo-app"
LIB = "demo-lib"
WIN = "demo-win"


def project(tmp, lock_mutator=None, extra_files=None, requirements="demo-app==1.0\n"):
    """A committed repository carrying the real release/ and dependency_audit/ code, a
    synthetic application, and a synthetic lock over a wheelhouse outside the repository.
    Returns a dict of paths and identities."""
    root, wheelhouse = os.path.join(tmp, "repo"), os.path.join(tmp, "wheelhouse")
    os.makedirs(wheelhouse)
    app = make_wheel(wheelhouse, APP, "1.0", requires=["demo-lib>=1.0", 'demo-win==1.0; sys_platform == "win32"'])
    lib = make_wheel(wheelhouse, LIB, "1.0")
    boot = shutil.copy(bundled_pip(), wheelhouse)
    requirements_bytes = requirements.encode("utf-8")
    lock = make_lock(requirements_bytes, boot, [(app, True, []), (lib, False, [APP])])
    if lock_mutator:
        lock = lock_mutator(lock, wheelhouse) or lock
    files = {"requirements.txt": requirements_bytes, "app.py": "print('demo')\n", lf.LOCK_PATH: lf.lock_bytes(lock)}
    for package in ("release", "dependency_audit"):
        for name in sorted(os.listdir(os.path.join(REPO, package))):
            if name.endswith(".py") and not name.startswith("tests_"):
                with open(os.path.join(REPO, package, name), "rb") as fh:
                    files["%s/%s" % (package, name)] = fh.read()
    for name in ("scanner-requirements.txt", "conformance.json"):
        with open(os.path.join(REPO, "dependency_audit", name), "rb") as fh:
            files["dependency_audit/" + name] = fh.read()
    with open(os.path.join(REPO, "dependency_audit", "policy.json"), "r", encoding="utf-8") as fh:
        policy = json.load(fh)
    policy["target"]["python"] = platform.python_version()
    files["dependency_audit/policy.json"] = json.dumps(policy, indent=2) + "\n"
    files[".gitignore"] = "/dependency_audit/evidence/\n__pycache__/\n"
    files.update(extra_files or {})
    commit, tree = init_repo(root, files)
    return {"root": root, "wheelhouse": wheelhouse, "lock": lock, "commit": commit, "tree": tree, "requirements": requirements_bytes}


def cli(root, python, *args, env=None):
    """Run the project's own copy of ``python -B -m release``."""
    return run([python, "-B", "-m", "release"] + list(args), cwd=root, env=clean_env(env))


def write_evidence(root, venv_python, vulns=None, now=NOW):
    """The real snapshot (the real CLI, in the environment) plus a scan answered in the
    real pip-audit JSON format — nothing is fetched. ``vulns``: {name: [advisory, ...]}."""
    from dependency_audit import core, orchestrate as oc, pip_adapter as pa
    proc = run([venv_python, "-m", "dependency_audit", "snapshot"], cwd=root, env=clean_env())
    if proc.returncode != 0:
        raise RuntimeError("snapshot refused: %s %s" % (proc.stdout, proc.stderr))
    evidence = os.path.join(root, "dependency_audit", "evidence")
    with open(os.path.join(evidence, "snapshot.json"), "r", encoding="utf-8") as fh:
        snap = json.load(fh)
    with open(os.path.join(root, "dependency_audit", "scanner-requirements.txt"), "r", encoding="utf-8") as fh:
        pins = pa.declared_requirements(fh.read())
    graphs = {"application": snap["packages"],
              "scanner": [{"name": n, "version": v, "scope": "tooling", "recordSha256": None, "path": "scanner:site-packages/%s" % n}
                          for n, v in sorted(pins.items())]}
    collection = {"schema": oc.COLLECTION_SCHEMA, "startedAt": now, "graphs": {}, "scanner": {"package": "pip-audit"},
                  "binding": snap["binding"], "removedEnvironment": [], "finishedAt": now}
    problems, findings = [], []
    for graph, pkgs in graphs.items():
        vulns_for = (vulns or {}) if graph == "application" else {}
        stdout = json.dumps({"dependencies": [{"name": p["name"], "version": p["version"], "vulns": vulns_for.get(p["name"], [])} for p in pkgs],
                             "fixes": []})
        status = 1 if any(vulns_for.get(p["name"]) for p in pkgs) else 0
        with open(os.path.join(evidence, "%s.inventory-requirements.txt" % graph), "w", encoding="utf-8") as fh:
            fh.write(pa.pins_text(pkgs))
        run_doc = oc._record_run(evidence, graph, {"command": "pip-audit", "args": ["-r", "x"], "status": status, "signal": None,
                                                   "timedOut": False, "error": None, "durationMs": 1, "stdout": stdout, "stderr": ""})
        collection["graphs"][graph] = {"run": run_doc, "inventorySha256": pa.inventory_digest(pkgs), "installed": len(pkgs),
                                       "packages": [{"name": p["name"], "version": p["version"], "scope": p["scope"],
                                                     "recordSha256": p["recordSha256"]} for p in pkgs]}
        found_problems, found = pa.read_report(graph, dict(run_doc, stdout=stdout), pkgs)
        problems += found_problems
        findings += found
    oc._write_json(os.path.join(evidence, "collection.json"), collection)
    result = core.evaluate(problems, findings, [], now)
    oc._write_json(os.path.join(evidence, "result.json"), dict({"schema": oc.RESULT_SCHEMA, "decidedAt": now,
                                                                "headline": core.headline(result)}, **result))
    return evidence


def ci_env(commit, event="push", ref="refs/heads/main", run_id="9001", attempt="1", outcomes=None, repository=lf.REPOSITORY):
    from . import candidate as cd
    steps = {s: {"outcome": "success", "conclusion": "success", "outputs": {}} for s in cd.REQUIRED_STEPS}
    for step, outcome in (outcomes or {}).items():
        steps[step] = {"outcome": outcome, "conclusion": outcome, "outputs": {}}
    return {"GITHUB_ACTIONS": "true", "GITHUB_SHA": commit, "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref,
            "GITHUB_REPOSITORY": repository, "GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": attempt, "GITHUB_RUN_NUMBER": "7",
            "GITHUB_JOB": "suite", "GITHUB_WORKFLOW_REF": "%s/.github/workflows/ci.yml@%s" % (repository, ref),
            "GITHUB_WORKFLOW_SHA": commit, "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "STEP_OUTCOMES": json.dumps(steps)}


def produce(tmp, **project_kwargs):
    """The whole producer, as CI runs it, against a synthetic project. Returns a dict with
    the project, the certified environment and the work paths; no candidate is packaged
    yet (tests package with the context they need)."""
    p = project(tmp, **project_kwargs)
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    base = base_python()
    steps = {}
    steps["observe"] = cli(p["root"], base, "observe", "--out", os.path.join(work, "before.json"))
    steps["install"] = cli(p["root"], base, "install", "--wheelhouse", p["wheelhouse"], "--venv", os.path.join(work, "venv"),
                           "--work", os.path.join(work, "install"))
    for name, proc in steps.items():
        if proc.returncode != 0:
            raise RuntimeError("%s refused: %s %s" % (name, proc.stdout, proc.stderr))
    venv_python = os.path.join(work, "venv", "bin", "python")
    write_evidence(p["root"], venv_python)
    return dict(p, work=work, venv=os.path.join(work, "venv"), python=venv_python, before=os.path.join(work, "before.json"),
                evidence=os.path.join(p["root"], "dependency_audit", "evidence"))


def package(state, out, env=None, local=False):
    args = ["package", "--wheelhouse", state["wheelhouse"], "--venv", state["venv"], "--evidence", state["evidence"],
            "--before", state["before"], "--out", out]
    if local:
        outcomes = os.path.join(state["work"], "outcomes.json")
        from . import candidate as cd
        with open(outcomes, "w", encoding="utf-8") as fh:
            json.dump({s: "success" for s in cd.REQUIRED_STEPS}, fh)
        args += ["--local", "--step-outcomes", outcomes]
    return cli(state["root"], state["python"], *args, env=env)


STARTUP_STAND_IN = '''import json, sys
sys.stdout.write(json.dumps({"ok": True, "planes": {"customer": {"detail": {"health": "ok", "imported": []}},
                                                  "admin": {"detail": {"health": "ok"}}}, "failures": []}))
'''
