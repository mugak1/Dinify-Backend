"""THE LOADED-PROCESS RELEASE IDENTITY (D08 B3) — which installed release THIS worker loaded.

Health says whether a process can answer. This says WHAT answered: the installed Backend
release a worker process loaded when it started, as stated by the trusted release
launcher that started it (``release/installation.py`` writes one per installed release).

HOW IT IS SET. The launcher resolves its OWN real path to the immutable installed release,
reads that release's installation receipt, checks the running interpreter's prefix and the
imported project package really live inside that same release, and calls ``install`` ONCE,
before Django loads. Nothing here reads a file, a mutable ``current`` pointer, an
environment variable, git or GitHub — so a later change to any of those cannot relabel a
process that is already running, and a worker keeps reporting the release it loaded until
it exits.

WHAT A PROCESS NOT STARTED BY THAT LAUNCHER SAYS. ``unavailable``. The legacy in-place
installation (one checkout and one virtual environment changed in place) has no launcher,
and its processes report exactly that — never a guessed commit. The promotion verifier
treats anything but ``verified`` for the expected release as a failed transition.

WHAT IS NEVER DISCLOSED: a filesystem path, a secret, a database destination, a full
inventory, a host name or an operator record. The published fields are bounded and
validated here, so a launcher cannot widen them by accident.
"""

from __future__ import annotations

import re
import threading

SCHEMA = "dinify.backend.runtime-identity/1"
PLANES = ("customer", "admin")
STATES = ("verified", "mismatch")

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE = re.compile(r"^[0-9a-f]{40}-[0-9a-f]{16}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_ID = re.compile(r"^[0-9]{1,20}$")
_WORD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")
_GROUP = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")

_lock = threading.Lock()
_loaded = None


def _check(value, pattern, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError("release identity field is not in its bounded form")
    return value


def _validated(doc):
    """The bounded public form, or ValueError. Unknown keys are refused, not dropped."""
    allowed = {"plane", "state", "reason", "release", "process"}
    if not isinstance(doc, dict) or set(doc) - allowed or doc.get("plane") not in PLANES or doc.get("state") not in STATES:
        raise ValueError("release identity is not a known shape")
    release = doc.get("release")
    process = doc.get("process") or {}
    if release is None and doc["state"] == "mismatch":
        release = {}   # a launcher that could not read its receipt still says so, with no release fields
    elif not isinstance(release, dict):
        raise ValueError("release identity carries no release")
    if set(release) - {"id", "commit", "tree", "environmentDigest", "recordSha256", "ciRun", "ciAttempt", "installedAt"} \
            or set(process) - {"instance", "loadedAt", "python", "modWsgi", "processGroup"}:
        raise ValueError("release identity carries an unknown field")
    out = {
        "schema": SCHEMA, "plane": doc["plane"], "state": doc["state"],
        "reason": _check(doc.get("reason"), _WORD, nullable=True),
        "release": None if not release else {
            "id": _check(release.get("id"), _RELEASE),
            "commit": _check(release.get("commit"), _SHA),
            "tree": _check(release.get("tree"), _SHA),
            "environmentDigest": _check(release.get("environmentDigest"), _HEX64),
            "recordSha256": _check(release.get("recordSha256"), _HEX64),
            "ciRun": _check(release.get("ciRun"), _ID, nullable=True),
            "ciAttempt": _check(release.get("ciAttempt"), _ID, nullable=True),
            "installedAt": _check(release.get("installedAt"), _TIME),
        },
        "process": {
            "instance": _check(process.get("instance"), _TOKEN),
            "loadedAt": _check(process.get("loadedAt"), _TIME),
            "python": _check(process.get("python"), _VERSION),
            "modWsgi": _check(process.get("modWsgi"), _VERSION, nullable=True),
            "processGroup": _check(process.get("processGroup"), _GROUP, nullable=True),
        },
    }
    if (out["state"] == "verified") != (out["reason"] is None):
        raise ValueError("a verified identity carries no reason; a mismatch always names one")
    return out


def install(doc):
    """Record this process's identity. Called once by the release launcher, before Django
    loads. A second call is refused: a process loads one release in its lifetime."""
    global _loaded
    validated = _validated(doc)
    with _lock:
        if _loaded is not None:
            raise RuntimeError("the release identity of this process is already set")
        _loaded = validated
    return validated


def current(plane):
    """What this process says about itself, for ``plane``. Never raises."""
    loaded = _loaded
    if loaded is None:
        return {"schema": SCHEMA, "plane": plane, "state": "unavailable", "reason": "not_started_by_release_launcher",
                "release": None, "process": None}
    if loaded["plane"] != plane:
        # Served by a plane other than the one the launcher started: say so rather than lend
        # one plane's identity to the other.
        return {"schema": SCHEMA, "plane": plane, "state": "mismatch", "reason": "plane_mismatch",
                "release": None, "process": None}
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in loaded.items()}


def _reset_for_tests():
    global _loaded
    with _lock:
        _loaded = None
