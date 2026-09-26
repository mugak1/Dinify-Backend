"""THE LOCK — the reviewed, target-specific, hash-locked resolution of the complete Python
dependency closure, and the rules that tie it to ``requirements.txt``.

``requirements.txt`` stays what it is: the DIRECT-INPUT CONTRACT, and still exactly what the
live UAT deploy installs from (unchanged by this delivery). ``release/python-lock.json`` is
the resolved closure for ONE declared target (CPython 3.12.3, Linux x86_64, glibc): every
package the application needs — the direct pins AND the transitives ``requirements.txt``
does not name (``cffi``, ``pycparser``) — each as one exact wheel file with its URL,
SHA-256 and size, plus the installer (``pip``) as a separately labelled bootstrap entry.

TWO SPECIFICATIONS MUST NEVER DRIFT APART SILENTLY. The lock records the SHA-256 of the
``requirements.txt`` it was generated from, and every consumer of the lock (the check, the
acquisition, the install, the packager and the reconstruction) refuses a lock whose
recorded digest is not the file's current digest. Editing ``requirements.txt`` therefore
invalidates the lock until it is deliberately regenerated and reviewed.

WHAT IS REVIEWED, WHAT IS MEASURED. Reviewed: this file and the lock (committed, diffable).
Measured: the SHA-256 and size of every downloaded file (against the lock, before
anything executes), the installed inventory (against the lock and the wheels' own
RECORDs, after installation). Routine certification CONSUMES the lock; it never resolves
against the index. Regeneration is a deliberate maintainer action (``lock generate``).

STRICTNESS. The direct inputs are parsed strictly — one ``name==version`` per line — so an
unpinned, duplicated, marker-qualified, extra-qualified, URL or option line is refused as a
form this lock does not support, rather than being dropped and leaving a requirement that
nothing enforces. Wheel compatibility is checked statically here against the declared
target (a fast, reviewable first gate); the authoritative answer is the target
interpreter's own supported-tag list, asked inside the environment at install time.

Pure: no network, no subprocess, no clock. Paths are joined by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse

LOCK_SCHEMA = "dinify.backend.python-lock/1"
LOCK_PATH = "release/python-lock.json"
REQUIREMENTS_PATH = "requirements.txt"
REPOSITORY = "mugak1/Dinify-Backend"
INDEX = "https://pypi.org/simple/"
FILE_HOST = "https://files.pythonhosted.org/packages/"

# The marker environment the lock was resolved for. platform_release / platform_version
# describe the generating host's KERNEL build and are deliberately not part of the target.
TARGET_MARKER_KEYS = (
    "implementation_name", "implementation_version", "os_name", "platform_machine",
    "platform_python_implementation", "platform_system", "python_full_version",
    "python_version", "sys_platform",
)

_LOCK_KEYS = ("schema", "repository", "target", "directInputs", "generator", "bootstrap", "packages")
_TARGET_KEYS = ("python", "implementation", "platform", "machine", "glibc", "markers")
_GENERATOR_KEYS = ("tool", "version", "wheelSha256", "index", "invocation", "resolvedAt", "reportSha256")
_BOOTSTRAP_KEYS = ("name", "version", "filename", "url", "sha256", "size")
_PACKAGE_KEYS = _BOOTSTRAP_KEYS + ("direct", "requiredBy")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_DIRECT_LINE = re.compile(r"^(%s)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)$" % _NAME)
_WHEEL = re.compile(
    r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!+]+)(?:-(?P<build>\d[A-Za-z0-9_.]*))?"
    r"-(?P<py>[A-Za-z0-9_.]+)-(?P<abi>[A-Za-z0-9_.]+)-(?P<plat>[A-Za-z0-9_.]+)\.whl$")
_MANYLINUX = re.compile(r"^manylinux_(\d+)_(\d+)_(\w+)$")
_LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize(name):
    """PEP 503 normalisation — the one form names are compared in."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _problem(code, detail):
    return {"code": code, "detail": detail}


# --- the direct-input contract -------------------------------------------------------

def parse_direct_inputs(text):
    """``requirements.txt`` as ``{normalized name: exact version}``, strictly.

    Returns ``(pins, problems)``. Blank lines and whole-line comments are the only lines
    that carry no requirement. Every other line must be exactly ``name==version``."""
    pins, problems = {}, []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _DIRECT_LINE.match(line)
        if not match:
            problems.append(_problem(
                "unsupported_direct_input",
                "requirements.txt line %d is not one exact `name==version` pin (markers, extras, ranges, URLs, "
                "options and inline comments are forms this lock does not support): %r" % (number, line[:120])))
            continue
        name = normalize(match.group(1))
        if name in pins:
            problems.append(_problem("duplicate_direct_input", "requirements.txt line %d pins %s a second time" % (number, name)))
            continue
        pins[name] = match.group(2)
    if not pins and not problems:
        problems.append(_problem("unsupported_direct_input", "requirements.txt declares no requirement"))
    return pins, problems


# --- wheels ----------------------------------------------------------------------------

def parse_wheel_filename(filename):
    """``{name, version, build, tags}`` for a PEP 427 filename, or None. ``tags`` is the
    expanded list of ``(python, abi, platform)`` triples a compressed tag set stands for."""
    match = _WHEEL.match(filename or "")
    if not match:
        return None
    tags = [(py, abi, plat)
            for py in match.group("py").split(".")
            for abi in match.group("abi").split(".")
            for plat in match.group("plat").split(".")]
    return {"name": normalize(match.group("name")), "version": match.group("version"),
            "build": match.group("build"), "tags": tags}


def _glibc(text):
    try:
        major, minor = str(text).split(".")
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


def tag_supported(tag, target):
    """The STATIC first gate: could ``tag`` run on the declared CPython/Linux target?

    Deliberately narrow — it recognises the tag families this closure uses and nothing
    else, so an unfamiliar tag is refused here and has to be reviewed. The authoritative
    answer is the target interpreter's own ``sys_tags()``, asked at install time."""
    py, abi, plat = tag
    major, minor = (int(x) for x in target["python"].split(".")[:2])
    cp = "cp%d%d" % (major, minor)
    glibc = _glibc(target.get("glibc"))
    machine = target["machine"]
    if abi == "none":
        py_ok = py in ("py%d" % major, "py%d%d" % (major, minor), cp)
    elif abi == cp:
        py_ok = py == cp
    elif abi == "abi3":
        m = re.match(r"^cp%d(\d+)$" % major, py)
        py_ok = bool(m) and int(m.group(1)) <= minor
    else:
        py_ok = False
    if not py_ok:
        return False
    if plat == "any":
        return True
    legacy = re.match(r"^(manylinux1|manylinux2010|manylinux2014)_(\w+)$", plat)
    modern = _MANYLINUX.match(plat)
    if legacy:
        need, arch = _LEGACY_MANYLINUX[legacy.group(1)], legacy.group(2)
    elif modern:
        need, arch = (int(modern.group(1)), int(modern.group(2))), modern.group(3)
    else:
        return False
    return arch == machine and glibc is not None and need <= glibc


def wheel_supported(filename, target):
    parsed = parse_wheel_filename(filename)
    return bool(parsed) and any(tag_supported(t, target) for t in parsed["tags"])


def required_glibc(filename):
    """The lowest glibc a wheel's Linux tags accept, or None for a pure wheel."""
    parsed = parse_wheel_filename(filename) or {"tags": []}
    needs = []
    for _, _, plat in parsed["tags"]:
        legacy = re.match(r"^(manylinux1|manylinux2010|manylinux2014)_", plat)
        modern = _MANYLINUX.match(plat)
        if legacy:
            needs.append(_LEGACY_MANYLINUX[legacy.group(1)])
        elif modern:
            needs.append((int(modern.group(1)), int(modern.group(2))))
    return "%d.%d" % min(needs) if needs else None


# --- the lock --------------------------------------------------------------------------

def entries(lock):
    """Every file the lock names, bootstrap first, each tagged with its role."""
    out = [dict(e, role="bootstrap") for e in lock.get("bootstrap") or []]
    out += [dict(e, role="application") for e in lock.get("packages") or []]
    return out


def _check_entry(entry, keys, where, target, problems):
    if not isinstance(entry, dict):
        problems.append(_problem("lock_invalid", "%s is not an object" % where))
        return False
    for key in entry:
        if key not in keys:
            problems.append(_problem("lock_invalid", '%s has unknown field "%s"' % (where, key)))
    for key in keys:
        if key not in entry:
            problems.append(_problem("lock_invalid", '%s is missing "%s"' % (where, key)))
    if any(key not in entry for key in keys):
        return False
    name, version, filename = entry["name"], entry["version"], entry["filename"]
    if not isinstance(name, str) or normalize(name) != name:
        problems.append(_problem("lock_invalid", "%s: name %r is not in normalized form" % (where, name)))
    if not isinstance(version, str) or not _VERSION.match(version):
        problems.append(_problem("lock_invalid", "%s: version %r is not one exact version" % (where, version)))
    parsed = parse_wheel_filename(filename) if isinstance(filename, str) else None
    if parsed is None:
        problems.append(_problem("lock_invalid", "%s: %r is not a wheel filename (sdists are not accepted: no build may run "
                                                 "on a consumer)" % (where, filename)))
    else:
        if parsed["name"] != name or parsed["version"] != version:
            problems.append(_problem("lock_invalid", "%s: %s names %s %s, the entry says %s %s"
                                     % (where, filename, parsed["name"], parsed["version"], name, version)))
        if target and not wheel_supported(filename, target):
            problems.append(_problem("wheel_incompatible", "%s: %s carries no tag the declared target (CPython %s, %s %s, glibc %s) "
                                     "supports" % (where, filename, target.get("python"), target.get("platform"),
                                                   target.get("machine"), target.get("glibc"))))
    url = entry["url"]
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (parts is None or not url.startswith(FILE_HOST) or parts.query or parts.fragment
            or urllib.parse.unquote(parts.path.rsplit("/", 1)[-1]) != filename):
        problems.append(_problem("lock_invalid", "%s: url must be the %s address of %s itself" % (where, FILE_HOST, filename)))
    if not isinstance(entry["sha256"], str) or not _SHA.match(entry["sha256"]):
        problems.append(_problem("lock_invalid", "%s: sha256 must be 64 lowercase hex digits" % where))
    size = entry["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        problems.append(_problem("lock_invalid", "%s: size must be a positive integer" % where))
    return True


def validate_lock(lock):
    """Structural validation of a parsed lock. Returns a list of problems."""
    problems = []
    if not isinstance(lock, dict):
        return [_problem("lock_invalid", "the lock is not a JSON object")]
    for key in lock:
        if key not in _LOCK_KEYS:
            problems.append(_problem("lock_invalid", 'unknown lock field "%s"' % key))
    if lock.get("schema") != LOCK_SCHEMA:
        problems.append(_problem("lock_invalid", "schema is not %s" % LOCK_SCHEMA))
    if lock.get("repository") != REPOSITORY:
        problems.append(_problem("lock_invalid", "repository is not %s" % REPOSITORY))

    target = lock.get("target")
    if not isinstance(target, dict) or sorted(target) != sorted(_TARGET_KEYS):
        problems.append(_problem("lock_invalid", "target must carry exactly %s" % ", ".join(_TARGET_KEYS)))
        target = None
    else:
        markers = target["markers"]
        if not (isinstance(target["python"], str) and re.match(r"^\d+\.\d+\.\d+$", target["python"])):
            problems.append(_problem("lock_invalid", "target.python must be one exact X.Y.Z version"))
            target = None
        elif (target["implementation"], target["platform"]) != ("CPython", "linux") or _glibc(target["glibc"]) is None:
            problems.append(_problem("lock_invalid", "this lock format declares CPython on Linux with a glibc X.Y version only"))
            target = None
        elif not isinstance(markers, dict) or sorted(markers) != sorted(TARGET_MARKER_KEYS) or \
                not all(isinstance(v, str) for v in markers.values()):
            problems.append(_problem("lock_invalid", "target.markers must carry exactly %s" % ", ".join(TARGET_MARKER_KEYS)))
            target = None
        elif (markers["python_full_version"], markers["platform_machine"], markers["sys_platform"],
              markers["platform_python_implementation"]) != (target["python"], target["machine"], target["platform"],
                                                             target["implementation"]):
            problems.append(_problem("lock_invalid", "target.markers contradict the declared target"))
            target = None

    direct = lock.get("directInputs")
    if not (isinstance(direct, dict) and sorted(direct) == ["path", "sha256"] and direct["path"] == REQUIREMENTS_PATH
            and isinstance(direct["sha256"], str) and _SHA.match(direct["sha256"])):
        problems.append(_problem("lock_invalid", "directInputs must be {path: %s, sha256}" % REQUIREMENTS_PATH))

    bootstrap = lock.get("bootstrap")
    if not isinstance(bootstrap, list) or len(bootstrap) != 1:
        problems.append(_problem("lock_invalid", "bootstrap must name exactly one installer (pip)"))
        bootstrap = []
    for i, entry in enumerate(bootstrap):
        if _check_entry(entry, _BOOTSTRAP_KEYS, "bootstrap[%d]" % i, target, problems) and entry["name"] != "pip":
            problems.append(_problem("lock_invalid", "bootstrap[%d] must be pip, the installer" % i))

    generator = lock.get("generator")
    if not isinstance(generator, dict) or sorted(generator) != sorted(_GENERATOR_KEYS):
        problems.append(_problem("lock_invalid", "generator must carry exactly %s" % ", ".join(_GENERATOR_KEYS)))
    else:
        pip_entry = bootstrap[0] if bootstrap and isinstance(bootstrap[0], dict) else {}
        if (generator["tool"], generator["version"], generator["wheelSha256"]) != ("pip", pip_entry.get("version"), pip_entry.get("sha256")):
            problems.append(_problem("lock_invalid", "generator must be the bootstrap pip itself (same version, same wheel)"))
        if generator["index"] != INDEX:
            problems.append(_problem("lock_invalid", "generator.index must be %s" % INDEX))
        if not (isinstance(generator["invocation"], list) and generator["invocation"] and all(isinstance(a, str) for a in generator["invocation"])):
            problems.append(_problem("lock_invalid", "generator.invocation must be the argument list that resolved the lock"))
        if not (isinstance(generator["resolvedAt"], str) and _ISO.match(generator["resolvedAt"])):
            problems.append(_problem("lock_invalid", "generator.resolvedAt must be an ISO-8601 UTC instant"))
        if not (isinstance(generator["reportSha256"], str) and _SHA.match(generator["reportSha256"])):
            problems.append(_problem("lock_invalid", "generator.reportSha256 must be 64 lowercase hex digits"))

    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        problems.append(_problem("lock_invalid", "packages must be a non-empty list"))
        packages = []
    good = []
    for i, entry in enumerate(packages):
        where = "packages[%d]" % i
        if not _check_entry(entry, _PACKAGE_KEYS, where, target, problems):
            continue
        if not isinstance(entry["direct"], bool):
            problems.append(_problem("lock_invalid", "%s: direct must be true or false" % where))
        required_by = entry["requiredBy"]
        if not (isinstance(required_by, list) and all(isinstance(n, str) for n in required_by)
                and required_by == sorted(set(required_by))):
            problems.append(_problem("lock_invalid", "%s: requiredBy must be a sorted list of unique names" % where))
            continue
        good.append(entry)

    names = [e.get("name") for e in bootstrap + packages if isinstance(e, dict)]
    seen = set()
    for name in names:
        if name in seen:
            problems.append(_problem("duplicate_package", "%s is locked more than once" % name))
        seen.add(name)
    app_names = [e["name"] for e in good]
    if app_names != sorted(app_names):
        problems.append(_problem("lock_invalid", "packages must be sorted by name, so a lock diff is reviewable"))
    locked = set(app_names)
    for entry in good:
        for parent in entry["requiredBy"]:
            if parent not in locked or parent == entry["name"]:
                problems.append(_problem("lock_invalid", "%s: requiredBy names %s, which is not another locked package"
                                         % (entry["name"], parent)))
        if entry["direct"] is False and not entry["requiredBy"]:
            problems.append(_problem("unexpected_package", "%s is neither a direct input nor required by a locked package "
                                     "on the declared target" % entry["name"]))
    return problems


def check_against_inputs(lock, requirements_bytes):
    """The lock was resolved FROM these direct inputs, and still agrees with them."""
    problems = []
    recorded = ((lock or {}).get("directInputs") or {}).get("sha256")
    current = sha256(requirements_bytes)
    if recorded != current:
        problems.append(_problem(
            "stale_lock",
            "requirements.txt is not the file the lock was generated from (lock records %s, the file is %s). "
            "Regenerate and review the lock (release/README.md); certification never regenerates it" % (recorded, current)))
    pins, parse_problems = parse_direct_inputs(requirements_bytes.decode("utf-8", "replace"))
    problems += parse_problems
    packages = {e.get("name"): e for e in (lock or {}).get("packages") or [] if isinstance(e, dict)}
    for name, version in sorted(pins.items()):
        entry = packages.get(name)
        if entry is None:
            problems.append(_problem("missing_direct", "%s==%s is a direct input with no locked file" % (name, version)))
        elif entry.get("version") != version:
            problems.append(_problem("direct_version_mismatch", "%s is locked at %s, requirements.txt pins %s"
                                     % (name, entry.get("version"), version)))
        elif entry.get("direct") is not True:
            problems.append(_problem("direct_flag_mismatch", "%s is a direct input but the lock does not mark it direct" % name))
    for name, entry in sorted(packages.items()):
        if entry.get("direct") is True and name not in pins:
            problems.append(_problem("direct_flag_mismatch", "%s is marked direct but requirements.txt does not pin it" % name))
    return problems


def check(lock_bytes, requirements_bytes):
    """Everything that can be checked offline about a lock. Returns ``(lock, problems)``;
    ``lock`` is None whenever a problem was found."""
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, [_problem("lock_unreadable", "%s: %s" % (LOCK_PATH, error))]
    problems = validate_lock(lock)
    if not problems:
        problems += check_against_inputs(lock, requirements_bytes)
    return (None if problems else lock), problems


def pip_requirements(selected):
    """The pip input for installing exactly ``selected`` lock entries: one exact pin and
    one hash per file. Derived, never reviewed on its own — the lock is the reviewed form."""
    return "".join("%s==%s --hash=sha256:%s\n" % (e["name"], e["version"], e["sha256"]) for e in selected)


def lock_bytes(lock):
    """The one canonical serialisation, so regeneration produces a clean diff."""
    return (json.dumps(lock, indent=2, sort_keys=False) + "\n").encode("utf-8")


def from_report(report, requirements_bytes, bootstrap, invocation, resolved_at, report_bytes, required_by, sizes, target):
    """Build a lock from pip's own installation report (``pip install --dry-run --report``).

    ``required_by`` maps each resolved name to the resolved names whose Requires-Dist, with
    markers evaluated in the target environment, names it; ``sizes`` maps filename to the
    measured size of the downloaded file. Returns ``(lock, problems)``."""
    problems = []
    if not isinstance(report, dict) or report.get("version") != "1" or not isinstance(report.get("install"), list):
        return None, [_problem("report_invalid", "not a pip installation report (version 1)")]
    pins, pin_problems = parse_direct_inputs(requirements_bytes.decode("utf-8", "replace"))
    problems += pin_problems
    packages = []
    for item in report["install"]:
        meta, info = item.get("metadata") or {}, item.get("download_info") or {}
        name, version, url = normalize(meta.get("name", "")), meta.get("version"), info.get("url", "")
        digest = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        if item.get("is_direct") or not url.startswith(FILE_HOST):
            problems.append(_problem("report_invalid", "%s was not resolved from the index" % name))
            continue
        if item.get("is_yanked"):
            problems.append(_problem("report_invalid", "%s %s is yanked" % (name, version)))
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        packages.append({
            "name": name, "version": version, "filename": filename, "url": url, "sha256": digest,
            "size": sizes.get(filename), "direct": name in pins, "requiredBy": sorted(set(required_by.get(name, []))),
        })
    packages.sort(key=lambda e: e["name"])
    environment = report.get("environment") or {}
    lock = {
        "schema": LOCK_SCHEMA,
        "repository": REPOSITORY,
        "target": dict(target, markers={k: environment.get(k) for k in TARGET_MARKER_KEYS}),
        "directInputs": {"path": REQUIREMENTS_PATH, "sha256": sha256(requirements_bytes)},
        "generator": {"tool": "pip", "version": bootstrap["version"], "wheelSha256": bootstrap["sha256"], "index": INDEX,
                      "invocation": list(invocation), "resolvedAt": resolved_at, "reportSha256": sha256(report_bytes)},
        "bootstrap": [{k: bootstrap[k] for k in _BOOTSTRAP_KEYS}],
        "packages": packages,
    }
    problems += validate_lock(lock)
    if not problems:
        problems += check_against_inputs(lock, requirements_bytes)
    return (None if problems else lock), problems
