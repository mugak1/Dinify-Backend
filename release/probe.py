"""THE PROBE — executed INSIDE a target virtual environment (``<venv>/bin/python -I
probe.py <mode>``), because only that interpreter can say what it supports and what is
installed in it. Standalone on purpose: stdlib plus the installer's own vendored
``packaging`` (``pip._vendor.packaging``), which is the exact code pip used to decide
markers, specifiers and wheel tags for the same environment.

It is always run from the CALLER's copy of this file — the producer's checkout, or the
consumer's own checkout — never from a candidate under examination.

Modes (a JSON request on stdin, a JSON answer on stdout):

    inspect  {"wheels": [filename...], "direct": {name: version}}
             interpreter facts, the marker environment, which of ``wheels`` this
             interpreter supports, every installed distribution with its RECORD, the
             files in site-packages no RECORD owns, and the closure reachable from
             ``direct`` through Requires-Dist with markers evaluated here.
    edges    {"distributions": [{"name", "version", "requires_dist": [...]}], "direct": [...]}
             which resolved names require which, with markers evaluated here — used only
             when a lock is generated.
"""

import csv
import io
import json
import os
import platform
import sys
import sysconfig

from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.version import InvalidVersion, Version


def facts():
    libc = platform.libc_ver()
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "libc": "%s %s" % libc if libc[0] else None,
        "soabi": sysconfig.get_config_var("SOABI"),
        "sysconfigPlatform": sysconfig.get_platform(),
        "prefix": os.path.realpath(sys.prefix),
        "basePrefix": os.path.realpath(sys.base_prefix),
        "executable": sys.executable,
    }


def marker_environment():
    return {k: v for k, v in default_environment().items() if isinstance(v, str)}


def _applies(req, extras):
    if req.marker is None:
        return True
    env = default_environment()
    for extra in [""] + sorted(extras):
        env["extra"] = extra
        if req.marker.evaluate(env):
            return True
    return False


def _parse(text, problems, owner):
    try:
        return Requirement(text)
    except InvalidRequirement as error:
        problems.append({"code": "requirement_unreadable", "detail": "%s declares an unreadable requirement %r (%s)" % (owner, text, error)})
        return None


def closure(dists, direct):
    """``dists``: {name: {"version", "requires": [...]}}. Returns (reachable, edges, problems)."""
    problems, edges = [], {}
    pending = [(canonicalize_name(n), frozenset()) for n in sorted(direct)]
    seen = {}
    while pending:
        name, extras = pending.pop(0)
        if extras <= seen.get(name, frozenset()) and name in seen:
            continue
        seen[name] = seen.get(name, frozenset()) | extras
        dist = dists.get(name)
        if dist is None:
            continue
        for text in dist["requires"]:
            req = _parse(text, problems, name)
            if req is None or not _applies(req, extras):
                continue
            child = canonicalize_name(req.name)
            edges.setdefault(child, set()).add(name)
            target = dists.get(child)
            if target is None:
                problems.append({"code": "missing_dependency", "detail": "%s requires %s, which is not installed" % (name, req)})
                continue
            try:
                satisfied = req.specifier.contains(Version(target["version"]), prereleases=True)
            except InvalidVersion:
                satisfied = False
            if not satisfied:
                problems.append({"code": "unsatisfied_dependency", "detail": "%s requires %s, installed %s" % (name, req, target["version"])})
            pending.append((child, frozenset(req.extras)))
    return sorted(seen), {k: sorted(v) for k, v in edges.items()}, problems


def _record_rows(dist_info):
    path = os.path.join(dist_info, "RECORD")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        rows = []
        for row in csv.reader(io.StringIO(fh.read())):
            if row:
                rows.append([row[0], row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else ""])
        return rows


def _metadata(dist_info):
    """(name, version, requires_dist) from METADATA, parsed the way the email-header
    format demands (a Requires-Dist value never wraps in practice, but a folded header is
    still read whole)."""
    from email.parser import HeaderParser
    with open(os.path.join(dist_info, "METADATA"), "r", encoding="utf-8") as fh:
        msg = HeaderParser().parse(fh)
    return msg.get("Name"), msg.get("Version"), msg.get_all("Requires-Dist") or []


def inspect(request):
    out = {"facts": facts(), "markers": marker_environment(), "problems": []}
    supported = set(sys_tags())
    out["wheelSupport"] = {}
    for filename in request.get("wheels") or []:
        try:
            _, _, _, tags = parse_wheel_filename(filename)
            out["wheelSupport"][filename] = any(t in supported for t in tags)
        except InvalidWheelFilename:
            out["wheelSupport"][filename] = False
    paths = sysconfig.get_paths()
    site = os.path.realpath(paths["purelib"])
    if os.path.realpath(paths["platlib"]) != site:
        out["problems"].append({"code": "layout_unsupported", "detail": "purelib and platlib differ"})
    out["sitePackages"] = site
    dists, owned = [], set()
    for entry in sorted(os.listdir(site)):
        full = os.path.join(site, entry)
        if not entry.endswith(".dist-info") or not os.path.isdir(full):
            continue
        try:
            name, version, requires = _metadata(full)
            rows = _record_rows(full)
        except (OSError, UnicodeDecodeError) as error:
            out["problems"].append({"code": "distribution_unreadable", "detail": "%s: %s" % (entry, error)})
            continue
        if not name or not version:
            out["problems"].append({"code": "distribution_unreadable", "detail": "%s has no name or version" % entry})
            continue
        for row in rows:
            owned.add(os.path.normpath(row[0]))
        installer = None
        try:
            with open(os.path.join(full, "INSTALLER"), "r", encoding="utf-8") as fh:
                installer = fh.read().strip()
        except OSError:
            pass
        dists.append({"name": canonicalize_name(name), "version": version, "distInfo": entry, "installer": installer,
                      "directUrl": os.path.exists(os.path.join(full, "direct_url.json")),
                      "requested": os.path.exists(os.path.join(full, "REQUESTED")),
                      "requires": requires, "record": rows})
    out["distributions"] = dists
    unowned = []
    for dirpath, dirnames, filenames in os.walk(site):
        dirnames.sort()
        for filename in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, filename), site)
            if rel in owned:
                continue
            parts = rel.split(os.sep)
            if len(parts) >= 2 and parts[-2] == "__pycache__" and filename.endswith(".pyc"):
                continue
            unowned.append(rel)
    out["unowned"] = unowned
    by_name = {d["name"]: {"version": d["version"], "requires": d["requires"]} for d in dists}
    reachable, edges, problems = closure(by_name, (request.get("direct") or {}).keys())
    out["reachable"], out["edges"] = reachable, edges
    out["problems"] += problems
    for name, version in sorted((request.get("direct") or {}).items()):
        have = by_name.get(canonicalize_name(name))
        if have is None:
            out["problems"].append({"code": "missing_direct", "detail": "%s is a direct input and is not installed" % name})
        elif have["version"] != version:
            out["problems"].append({"code": "direct_version_mismatch", "detail": "%s %s installed, %s pinned" % (name, have["version"], version)})
    return out


def edges(request):
    dists = {canonicalize_name(d["name"]): {"version": d["version"], "requires": d.get("requires_dist") or []}
             for d in request["distributions"]}
    reachable, found, problems = closure(dists, request.get("direct") or [])
    return {"markers": marker_environment(), "facts": facts(), "reachable": reachable, "requiredBy": found, "problems": problems}


def main(argv):
    if len(argv) != 1 or argv[0] not in ("inspect", "edges"):
        sys.stderr.write("usage: python -I probe.py <inspect|edges> < request.json\n")
        return 64
    request = json.load(sys.stdin)
    answer = inspect(request) if argv[0] == "inspect" else edges(request)
    sys.stdout.write(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
