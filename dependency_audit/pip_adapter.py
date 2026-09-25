"""THE pip ADAPTER — what was inspected, how the scanner is installed and invoked, and how
its answer is read. Everything here either produces evidence or refuses.

WHAT IS AUDITED. Not ``requirements.txt`` resolved afresh: the package inventory of the
Python environment the suite actually ran in, read with the target interpreter's own
``pip inspect``. ``pip-audit -r requirements.txt`` resolves dependencies on its own (it
does NOT ignore transitives), but an independent resolution is not the inventory that was
validated — the unpinned transitives (``cffi``, ``pycparser``) and the ``pip`` CI upgrades
to latest are all in the real environment and all in this inventory. That inventory is
written out as exact ``name==version`` pins and audited with ``--no-deps --disable-pip
--strict``, so pip-audit resolves nothing and may skip nothing.

WHERE THE SCANNER LIVES. In its own virtual environment, created fresh from the target
interpreter and installed from ``scanner-requirements.txt`` with ``--require-hashes
--no-deps --only-binary=:all: --isolated``. Installing it cannot move a target package —
and the target inventory is compared before and after to prove it. The scanner's own
inventory is then audited as a second graph: it executes in CI, so it is tooling too (its
bundled ``pip 24.0`` would otherwise carry twelve advisory entries — it is pinned to
26.2.1 in the requirements file for exactly that reason).

pip-audit reports NO severity. Every finding therefore arrives as severity "unknown",
which the policy core treats conservatively: blocking on a runtime package, unresolvable
(incomplete) on tooling. It is never read as low.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys

TOOLING = frozenset({"pip", "setuptools", "wheel"})
PYPI_INDEX = "https://pypi.org/simple/"


def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize(name):
    """PEP 503 normalisation: the form pip-audit reports names in."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def environment_facts():
    libc = platform.libc_ver()
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "libc": "%s %s" % libc if libc[0] else None,
    }


def scrubbed_environment(base=None):
    """pip / pip-audit / interpreter configuration that could narrow, redirect or inject
    into an install or a scan is removed, never inherited."""
    base = dict(os.environ if base is None else base)
    env, removed = {}, []
    for key, value in base.items():
        if key.upper().startswith(("PIP_", "PYTHON")) or key in ("VIRTUAL_ENV", "CONDA_PREFIX"):
            removed.append(key)
            continue
        env[key] = value
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env, sorted(removed)


def parse_inspect(stdout, graph):
    """Read ``pip inspect`` output. Returns (packages, problems)."""
    problems = []
    try:
        doc = json.loads(stdout)
    except (TypeError, ValueError) as error:
        return [], [{"code": "inventory_unreadable", "detail": "%s: pip inspect output is not JSON (%s)" % (graph, error)}]
    installed = doc.get("installed") if isinstance(doc, dict) else None
    if not isinstance(installed, list):
        return [], [{"code": "inventory_unreadable", "detail": "%s: pip inspect reported no installed list" % graph}]
    packages, seen = [], set()
    for item in installed:
        meta = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not isinstance(meta.get("version"), str):
            problems.append({"code": "inventory_unreadable", "detail": "%s: an installed distribution has no name/version" % graph})
            continue
        name = normalize(meta["name"])
        if name in seen:
            problems.append({"code": "inventory_duplicate", "detail": "%s: %s is installed more than once" % (graph, name)})
        seen.add(name)
        location = item.get("metadata_location")
        record_sha = None
        if isinstance(location, str):
            for candidate in ("RECORD", "PKG-INFO", "METADATA"):
                path = os.path.join(location, candidate)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        record_sha = sha256(fh.read())
                    break
        if item.get("direct_url") is not None:
            problems.append({"code": "not_from_index", "detail": "%s: %s was installed from a direct URL or path; no index advisory can describe it" % (graph, name)})
        packages.append({
            "path": "%s:site-packages/%s" % (graph, name),
            "name": name,
            "version": meta["version"],
            "installer": item.get("installer"),
            "recordSha256": record_sha,
            "scope": None,
        })
    packages.sort(key=lambda p: p["name"])
    if not packages:
        problems.append({"code": "empty_inventory", "detail": "%s: the environment has no installed packages" % graph})
    return packages, problems


def inventory_digest(packages):
    return sha256("\n".join("%s\0%s\0%s" % (p["name"], p["version"], p["recordSha256"]) for p in packages))


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#\\]+)")
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared_requirements(text):
    """Top-level requirement lines as {name: pinned version or None}."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith("--"):
            continue
        pin = _PIN.match(line)
        if pin:
            out[normalize(pin.group(1))] = pin.group(2)
            continue
        bare = _NAME.match(line)
        if bare:
            out[normalize(bare.group(1))] = None
    return out


def check_declared(packages, declared, graph):
    """Every declared requirement is installed, at its pin when it has one."""
    problems = []
    by_name = {p["name"]: p for p in packages}
    for name, version in sorted(declared.items()):
        if name not in by_name:
            problems.append({"code": "missing_declared", "detail": "%s: %s is declared but not installed" % (graph, name)})
        elif version is not None and by_name[name]["version"] != version:
            problems.append({"code": "requirement_not_satisfied", "detail": "%s: %s is %s installed, %s declared" % (graph, name, by_name[name]["version"], version)})
    return problems


def application_scope(package):
    return "tooling" if package["name"] in TOOLING else "runtime"


def tooling_scope(_package):
    return "tooling"


def pins_text(packages):
    """The inventory as exact pins — what pip-audit is asked about, and nothing else."""
    return "".join("%s==%s\n" % (p["name"], p["version"]) for p in packages)


def scanner_args(requirements_path, cache_dir, timeout):
    return ["-r", requirements_path, "--no-deps", "--disable-pip", "--strict", "--format", "json",
            "--vulnerability-service", "pypi", "--progress-spinner", "off", "--timeout", str(timeout),
            "--cache-dir", cache_dir]


def read_report(graph, run, packages):
    """Read ONE pip-audit JSON answer. Returns (problems, findings).

    The exit status is CROSS-CHECKED against the body: pip-audit exits 1 both for "known
    vulnerabilities found" and for a failure, so a status is accepted only when the body
    agrees with it.
    """
    problems, findings = [], []

    def fail(code, detail):
        problems.append({"code": code, "detail": "%s: %s" % (graph, detail)})
        return problems, findings

    if run.get("error"):
        return fail("scanner_error", "the scanner could not be run (%s)" % run["error"])
    if run.get("timedOut"):
        return fail("scanner_timeout", "the scanner did not finish within the policy timeout")
    if run.get("signal"):
        return fail("scanner_signal", "the scanner was killed by %s" % run["signal"])
    status = run.get("status")
    if status not in (0, 1):
        return fail("scanner_status", "the scanner exited %s" % status)
    text = run.get("stdout") or ""
    if text.strip() == "":
        return fail("scanner_empty", "the scanner produced no output — an empty report is not a clean one")
    try:
        report = json.loads(text)
    except ValueError as error:
        return fail("scanner_unparseable", "the scanner output is not JSON (%s)" % error)
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        return fail("scanner_shape", "the report lacks a dependencies list")
    deps = report["dependencies"]
    if not deps:
        return fail("coverage_empty", "the scanner reported no dependencies — an empty inventory is refused, not passed")

    by_name = {p["name"]: p for p in packages}
    reported = set()
    any_vuln = False
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            problems.append({"code": "scanner_shape", "detail": "%s: a dependency entry is malformed" % graph})
            continue
        name = normalize(dep["name"])
        if name in reported:
            problems.append({"code": "scanner_shape", "detail": "%s: %s is reported twice" % (graph, name)})
        reported.add(name)
        if dep.get("skip_reason"):
            problems.append({"code": "coverage_skipped", "detail": "%s: %s was not audited (%s)" % (graph, name, dep.get("skip_reason"))})
            continue
        pkg = by_name.get(name)
        if pkg is None:
            problems.append({"code": "coverage_unknown_node", "detail": "%s: %s is reported but is not in the audited inventory" % (graph, name)})
            continue
        if dep.get("version") != pkg["version"]:
            problems.append({"code": "coverage_mismatch", "detail": "%s: %s reported at %s, inventory has %s" % (graph, name, dep.get("version"), pkg["version"])})
            continue
        vulns = dep.get("vulns")
        if not isinstance(vulns, list):
            problems.append({"code": "scanner_shape", "detail": "%s: %s has no vulns list" % (graph, name)})
            continue
        merged = {}
        for v in vulns:
            if not isinstance(v, dict) or not isinstance(v.get("id"), str) or not v.get("id"):
                problems.append({"code": "scanner_shape", "detail": "%s: an advisory on %s has no identifier" % (graph, name)})
                continue
            entry = merged.setdefault(v["id"], {"aliases": set(), "description": v.get("description") or ""})
            for alias in v.get("aliases") or []:
                if isinstance(alias, str):
                    entry["aliases"].add(alias)
        for advisory in sorted(merged):
            any_vuln = True
            entry = merged[advisory]
            findings.append({
                "advisory": advisory,
                "aliases": sorted(entry["aliases"] - {advisory}),
                "package": name,
                "version": pkg["version"],
                "path": pkg["path"],
                "scope": pkg["scope"],
                "severity": "unknown",
                "title": entry["description"][:200],
                "url": "",
            })
    missing = sorted(set(by_name) - reported)
    for name in missing:
        problems.append({"code": "coverage_mismatch", "detail": "%s: %s is in the inventory but the scanner did not report it" % (graph, name)})
    if status == 0 and any_vuln:
        problems.append({"code": "scanner_inconsistent", "detail": "%s: the scanner exited 0 but listed vulnerabilities" % graph})
    if status == 1 and not any_vuln and not problems:
        problems.append({"code": "scanner_status", "detail": "%s: the scanner exited 1 with no vulnerabilities and no error — an unexplained failure" % graph})
    return problems, findings
