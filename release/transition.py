"""THE TRANSITION (D08 B3) — move BOTH WSGI planes from whatever they serve now to one
installed, verified release, deliberately, and report what each process actually loaded.

    under the shared host lock (bounded wait), with a journal for this operation:
      recheck   the host, the admission's deadline, the installed release, identity support
      gate      each plane's configuration probed as the runtime identity
      plan      the migration plan read as the migration identity, decided against the
                TRUSTED reviewed decisions; anything unreviewed, contracting or unknown stops
      migrate   only a reviewed expand plan, before any switch (old code tolerates it)
      switch    both planes' include files replaced (the previous bytes kept), configtest,
                then ONE graceful or restart reload
      verify    every sample from each plane reports the target release, loaded by this
                release's launcher; every daemon process of each plane has its working
                directory and its mapped files in the target release; both health routes
                pass (the customer's with its database connected)
      restore   on ANY failure after the switch: the previous includes back, configtest,
                reload, and the previous state re-verified. A failed transition is reported
                as failed even when the restoration succeeds; a failed restoration is
                reported as that, and the operation stays open for ``resume``.

WHAT SWITCHES. Each plane's Apache include (``<includeDir>/dinify-backend-<plane>.conf``) is
the ONLY file this module writes outside the release root and its state directory. It holds
that plane's WSGIDaemonProcess (python-home, home and python-path pinned to one release) and
WSGIScriptAlias. The vhost files that Include them are never edited here.

WHAT OLD WORKERS DO. Apache's reload replaces each daemon group's processes; a request already
running in an old process continues with the old release's interpreter, packages and source,
all still present and immutable, so a lazy import there resolves to the release that process
started from. That is not zero downtime, and the window is SHORT: measured on the rehearsal
host (Apache 2.4 + mod_wsgi 5.0, Ubuntu 24.04), a graceful reload reclaimed the previous
daemon processes about 3 seconds after the signal whatever ``shutdown-timeout`` said, so a
request still running then was cut (the client saw a 500). The restart mode drops connections
outright, as the legacy deploy's ``systemctl restart apache2`` does today.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.parse

from . import environment as ev
from . import hostprofile as hp
from . import installation as ins

INCLUDE = "dinify-backend-%s.conf"
HEADER = re.compile(rb"^# dinify-backend-release: ([0-9a-f]{40}-[0-9a-f]{16}) operation: ([a-z0-9-]+)\n")
DECISIONS = os.path.join("release", "migration-decisions.json")
DECISIONS_SCHEMA = "dinify.backend.migration-decisions/1"
LOCK_WAIT_SECONDS = 300
STAGES = ("admitted", "prepared", "locked", "unchanged", "gated", "migrated", "switching", "switched", "verified",
          "verification-failed", "restored", "restoration-failed", "refused", "resumed")
HOSTPROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hostprobe.py")


def _problem(code, detail):
    return {"code": code, "detail": detail}


# --- the shared host lock (the contract Admin's promotion joins) ---------------------------

class HostLock:
    """ONE mutation of this host's serving configuration at a time — Backend and Admin alike.
    ``flock(2)`` on ``profile.lockPath``: released by the kernel if the holder dies, so a
    killed runner never leaves it held. Waiting is bounded; timing out changes nothing."""

    def __init__(self, path, holder, wait=LOCK_WAIT_SECONDS):
        self.path, self.holder, self.wait, self.fd = path, holder, wait, None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise TimeoutError("the host lock %s stayed held for %ds (held by: %s)" % (self.path, self.wait, _read_holder(self.path)))
                time.sleep(1)
        ins._write_json(self.path + ".holder", {"holder": self.holder, "since": ins.now_iso(), "pid": os.getpid()}, mode=0o600)
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path + ".holder")
        except OSError:
            pass
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _read_holder(path):
    doc, _ = ins.read_json(path + ".holder")
    return "%s since %s" % (doc.get("holder"), doc.get("since")) if isinstance(doc, dict) else "unknown"


# --- the journal ----------------------------------------------------------------------------

class Journal:
    """``<stateDir>/operations/<operation>.jsonl`` (append-only) and ``<stateDir>/active.json``
    while an operation is open. An open operation left by a killed process blocks the next
    one until ``resume`` settles it: the state it left cannot be assumed."""

    def __init__(self, profile, operation):
        self.dir = os.path.join(profile["stateDir"], "operations")
        self.path = os.path.join(self.dir, "%s.jsonl" % operation)
        self.active = os.path.join(profile["stateDir"], "active.json")
        self.operation = operation

    def open(self, detail):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        current, _ = ins.read_json(self.active)
        if isinstance(current, dict) and current.get("operation") not in (None, self.operation):
            return [_problem("previous_operation_unresolved", "operation %s is still open (stage %s); run `host resume --operation %s` "
                             "to establish what it left serving before anything else changes" % (current.get("operation"), current.get("stage"), current.get("operation")))]
        ins._write_json(self.active, {"operation": self.operation, "openedAt": ins.now_iso(), "stage": "locked"}, mode=0o600)
        self.record("locked", detail)
        return []

    def record(self, stage, detail=None):
        assert stage in STAGES, stage
        line = json.dumps({"at": ins.now_iso(), "operation": self.operation, "stage": stage, "detail": detail}, sort_keys=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(self.active):
            doc, _ = ins.read_json(self.active)
            if isinstance(doc, dict) and doc.get("operation") == self.operation:
                ins._write_json(self.active, dict(doc, stage=stage), mode=0o600)

    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def close(self):
        doc, _ = ins.read_json(self.active)
        if isinstance(doc, dict) and doc.get("operation") == self.operation:
            os.remove(self.active)

    def keep(self, name, data):
        path = os.path.join(self.dir, self.operation)
        os.makedirs(path, mode=0o700, exist_ok=True)
        with open(os.path.join(path, name), "wb") as fh:
            fh.write(data)


# --- the include files ----------------------------------------------------------------------

def include_path(profile, plane):
    return os.path.join(profile["apache"]["includeDir"], INCLUDE % plane)


def render_include(profile, plane, rid, operation):
    """The plane's serving directives, pinned to ONE installed release by its real path."""
    p, rel = profile["planes"][plane], os.path.join(profile["releaseRoot"], rid)
    ids = profile["identities"]
    lines = [
        "# dinify-backend-release: %s operation: %s" % (rid, operation),
        "# Generated by the Dinify Backend release transition (D08 B3). Do not edit: the next transition",
        "# replaces this file whole and keeps the previous one in its journal.",
    ]
    if p["staticUrl"]:
        lines += ["Alias %s %s/static/" % (p["staticUrl"].rstrip("/") + "/", rel),
                  "<Directory %s/static>" % rel, "    Require all granted", "</Directory>"]
    if plane == "customer" and profile["media"]["url"]:
        lines += ["Alias %s %s" % (profile["media"]["url"].rstrip("/") + "/", profile["media"]["root"].rstrip("/") + "/"),
                  "<Directory %s>" % profile["media"]["root"].rstrip("/"), "    Require all granted", "</Directory>"]
    lines += [
        "WSGIDaemonProcess %s user=%s group=%s processes=%d threads=%d display-name=%%{GROUP} lang=C.UTF-8 locale=C.UTF-8 \\"
        % (p["daemon"], ids["runtime"], ids["runtimeGroup"], p["processes"], p["threads"]),
        "    home=%s/source python-home=%s/venv python-path=%s/source:%s/wsgi shutdown-timeout=%d"
        % (rel, rel, rel, rel, p["shutdownTimeout"]),
        "WSGIScriptAlias %s %s/wsgi/%s.wsgi process-group=%s application-group=%%{GLOBAL}" % (p["mount"], rel, plane, p["daemon"]),
        "WSGIPassAuthorization %s" % ("On" if p["passAuthorization"] else "Off"),
        "<Directory %s/wsgi>" % rel, "    <Files %s.wsgi>" % plane, "        Require all granted", "    </Files>", "</Directory>",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def read_include(profile, plane):
    """``(bytes, release id or None)``; None is a legacy (pre-B3) include."""
    with open(include_path(profile, plane), "rb") as fh:
        data = fh.read()
    match = HEADER.match(data)
    return data, (match.group(1).decode() if match else None)


def _replace(path, data):
    tmp = "%s.next-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def apache(profile, action):
    return ev.run([profile["apache"]["ctl"], action], env=ev.scrubbed_env({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}), timeout=120)


# --- observation of what is serving ---------------------------------------------------------

def fetch(base, connect_to, path, timeout=10):
    """GET ``base``+``path`` over a connection to ``connect_to`` (loopback), with the base's
    host name sent as Host and SNI — ``curl --resolve`` without a subprocess."""
    url = urllib.parse.urlsplit(base.rstrip("/") + path)
    port = url.port or (443 if url.scheme == "https" else 80)
    if url.scheme == "https":
        conn = http.client.HTTPSConnection(url.hostname, port, timeout=timeout, context=ssl.create_default_context())
        conn.sock = conn._context.wrap_socket(socket.create_connection((connect_to, port), timeout), server_hostname=url.hostname)
    else:
        conn = http.client.HTTPConnection(url.hostname, port, timeout=timeout)
        conn.sock = socket.create_connection((connect_to, port), timeout)
    try:
        conn.request("GET", url.path, headers={"Host": url.netloc, "Connection": "close", "Accept": "application/json"})
        response = conn.getresponse()
        body = response.read(65536)
        try:
            doc = json.loads(body.decode("utf-8"))
        except ValueError:
            doc = None
        return response.status, doc, response.getheader("Cache-Control")
    finally:
        conn.close()


TITLE_MIN = 8   # "(wsgi:" plus at least two characters of the group


def title_matches(argv0, group, others=()):
    """mod_wsgi names a daemon process ``(wsgi:<group>)`` (display-name=%{GROUP}) by writing
    over the PARENT's argv[0], so the title is TRUNCATED to that length — measured on Ubuntu
    24.04: ``/usr/sbin/apache2`` leaves ``(wsgi:dinify-cust``. A truncated title is accepted as
    a prefix of the full one only when no other group's title shares that prefix."""
    title = "(wsgi:%s)" % group
    if len(argv0) < TITLE_MIN or not title.startswith(argv0):
        return False
    return not any(("(wsgi:%s)" % other).startswith(argv0) for other in others if other != group)


def daemon_processes(group, others=()):
    """Every live process titled for ``group``, with its working directory and the distinct
    files it has mapped."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                argv0 = fh.read().split(b"\0")[0].decode("utf-8", "replace").strip()
            if not title_matches(argv0, group, others):
                continue
            cwd = os.readlink("/proc/%s/cwd" % pid)
            with open("/proc/%s/maps" % pid, encoding="utf-8", errors="replace") as fh:
                mapped = {line.split(None, 5)[5].strip() for line in fh if len(line.split(None, 5)) == 6}
        except OSError:
            continue
        found.append({"pid": int(pid), "cwd": cwd, "mapped": sorted(m for m in mapped if m.startswith("/"))})
    return found


def include_group(data):
    """The daemon group an include defines (``WSGIDaemonProcess <name> ...``), or None."""
    match = re.search(rb"^\s*WSGIDaemonProcess\s+(\S+)", data, re.M)
    return match.group(1).decode() if match else None


def plane_evidence(profile, plane, expect, groups):
    """What ONE plane is serving now. ``expect`` is a release id, or None for a legacy
    include (whose processes can only be shown to be serving, never which code they loaded).
    ``groups`` maps each plane to the daemon group its include defines."""
    p = profile["planes"][plane]
    base, connect = p["probe"]["base"], p["probe"]["connectTo"]
    problems, samples = [], []
    try:
        status, body, _ = fetch(base, connect, hp.PLANE_PATHS[plane]["health"])
    except OSError as error:
        status, body = None, None
        problems.append(_problem("health_failed", "%s health did not answer (%s)" % (plane, type(error).__name__)))
    health = {"status": status, "body": body}
    if status is not None:
        ok = status == 200 and isinstance(body, dict) and body.get("status") == "ok" and \
            (plane != "customer" or body.get("database") == "connected")
        if not ok:
            problems.append(_problem("health_failed", "%s health answered %s %s" % (plane, status, json.dumps(body)[:200])))
    root = profile["releaseRoot"].rstrip("/") + "/"
    for _ in range(max(8, 4 * p["processes"])):
        try:
            status, body, cache = fetch(base, connect, hp.PLANE_PATHS[plane]["identity"])
        except OSError as error:
            problems.append(_problem("identity_failed", "%s identity did not answer (%s)" % (plane, type(error).__name__)))
            break
        samples.append({"status": status, "state": (body or {}).get("state"), "release": ((body or {}).get("release") or {}).get("id"),
                        "instance": ((body or {}).get("process") or {}).get("instance"), "cacheControl": cache,
                        "reason": (body or {}).get("reason")})
    for s in samples:
        if expect is None:
            if s["state"] == "verified":
                problems.append(_problem("identity_unexpected", "%s reports release %s where the legacy installation was expected" % (plane, s["release"])))
        elif s["status"] != 200 or s["state"] != "verified" or s["release"] != expect or "no-store" not in (s["cacheControl"] or ""):
            problems.append(_problem("identity_mismatch", "%s answered %s state=%s release=%s reason=%s; %s is required"
                                     % (plane, s["status"], s["state"], s["release"], s["reason"], expect)))
            break
    processes = daemon_processes(groups[plane], [g for k, g in groups.items() if k != plane])
    rel = root + expect if expect else None
    for proc in processes:
        releases = sorted({m[len(root):].split("/", 1)[0] for m in proc["mapped"] if m.startswith(root)})
        proc["releases"] = releases
        del proc["mapped"]
        if expect is None:
            if releases:
                problems.append(_problem("process_mismatch", "%s process %d maps files from installed releases %s" % (plane, proc["pid"], releases)))
        elif proc["cwd"] != rel + "/source" or releases != [expect]:
            problems.append(_problem("process_mismatch", "%s process %d runs in %s with files from %s; only %s may be loaded"
                                     % (plane, proc["pid"], proc["cwd"], releases, expect)))
    if not processes:
        problems.append(_problem("process_missing", "no %s daemon process (wsgi:%s) is running" % (plane, groups[plane])))
    instances = sorted({s["instance"] for s in samples if s["instance"]})
    return {"plane": plane, "expect": expect, "health": health, "samples": len(samples), "instances": instances,
            "processes": processes}, problems


def verify_serving(profile, expect, budget, groups=None):
    """Both planes, retried until ``budget`` seconds pass (old processes drain within their
    shutdown-timeout). ``expect`` is ``{plane: release id or None}``; ``groups`` the daemon
    group each plane's include defines (the profile's, unless a legacy include says otherwise)."""
    groups = groups or {p: profile["planes"][p]["daemon"] for p in hp.PLANES}
    deadline = time.monotonic() + budget
    while True:
        evidence, problems = {}, []
        for plane in hp.PLANES:
            evidence[plane], found = plane_evidence(profile, plane, expect[plane], groups)
            problems += found
        if not problems or time.monotonic() >= deadline:
            return evidence, problems
        time.sleep(1)


def _budget(profile):
    return profile["apache"]["drainSeconds"] + max(profile["planes"][p]["shutdownTimeout"] for p in hp.PLANES) + 30


# --- the gates ------------------------------------------------------------------------------

def identity_support(release):
    """A release this transition may promote must publish its loaded identity. Read as files;
    nothing from the release executes as root."""
    need = {"source/dinify_backend/release_identity.py": b"dinify.backend.runtime-identity/1",
            "source/misc_app/endpoints/release_identity.py": b"ReleaseIdentityView",
            "source/dinify_backend/urls.py": b"api/v1/release/", "source/platform_admin_app/urls.py": b"'release/'"}
    missing = []
    for rel, marker in sorted(need.items()):
        try:
            with open(os.path.join(release, rel), "rb") as fh:
                if marker not in fh.read():
                    missing.append(rel)
        except OSError:
            missing.append(rel)
    if missing:
        return [_problem("identity_unsupported", "the release does not publish a loaded-process identity (%s): it predates B3 and "
                         "cannot be promoted, because nothing could verify what its processes loaded" % ", ".join(missing))]
    return []


def probe(profile, role, release, mode, request, trusted_root):
    step = ins.run_as(profile, role, [os.path.join(release, "venv", "bin", "python"), "-I", os.path.join(trusted_root, "release", "hostprobe.py"), mode],
                      cwd="/", env=ev.scrubbed_env({"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"}), timeout=300,
                      stdin=json.dumps(dict(request, release=release)))
    try:
        answer = json.loads(step["stdout"])
    except ValueError:
        return None, [_problem("probe_failed", "the %s probe answered nothing readable (status %s)" % (mode, step["status"]))]
    return answer, list(answer.get("problems") or [])


def config_gate(profile, release, trusted_root):
    evidence, problems = {}, []
    for plane in hp.PLANES:
        answer, found = probe(profile, "runtime", release, "config", {
            "plane": plane, "configPath": profile["planes"][plane]["config"], "mediaRoot": profile["media"]["root"],
            "otpAllowed": profile["otp"]["deterministicTestOtpAllowed"]}, trusted_root)
        evidence[plane] = {k: v for k, v in (answer or {}).items() if k != "problems"}
        problems += found
    return evidence, problems


def load_decisions(trusted_root):
    path = os.path.join(trusted_root, DECISIONS)
    doc, err = ins.read_json(path)
    if err or not isinstance(doc, dict) or doc.get("schema") != DECISIONS_SCHEMA or not isinstance(doc.get("decisions"), dict):
        return None, [_problem("decisions_invalid", "%s is not a %s document" % (DECISIONS, DECISIONS_SCHEMA))]
    problems = []
    for mid, d in sorted(doc["decisions"].items()):
        if not (isinstance(d, dict) and re.match(r"^[0-9a-f]{64}$", str(d.get("sha256"))) and d.get("class") in ("expand", "contract", "data")
                and re.match(r"^mugak1/Dinify-Backend#[0-9]+$", str(d.get("reviewedIn"))) and isinstance(d.get("operations"), list)):
            problems.append(_problem("decisions_invalid", "the decision for %s is incomplete" % mid))
    return doc["decisions"], problems


def decide_migrations(plan, decisions):
    """A migration is applied automatically ONLY when a reviewed decision names its exact file
    (by digest) and exact operations as ``expand``; the evidence may refuse a decision (a
    destructive operation), never widen one. Returns ``(to_apply, problems)``."""
    from .hostprobe import DESTRUCTIVE
    problems, apply = [], []
    if plan.get("conflicts"):
        problems.append(_problem("migration_conflict", "the migration graph has conflicts: %s" % "; ".join(plan["conflicts"])))
    for step in plan.get("pending") or []:
        d = decisions.get(step["id"])
        if step.get("backwards"):
            problems.append(_problem("migration_backwards", "%s would be unapplied; this path never reverses a migration" % step["id"]))
        elif d is None:
            problems.append(_problem("migration_unreviewed", "%s (%s) has no reviewed compatibility decision; the automated path stops. "
                                     "Add one in a reviewed change, or plan a maintenance window" % (step["id"], ", ".join(step["operations"]))))
        elif d["sha256"] != step["sha256"] or d["operations"] != step["operations"]:
            problems.append(_problem("migration_decision_stale", "the decision for %s names another file or other operations" % step["id"]))
        elif d["class"] != "expand":
            problems.append(_problem("migration_requires_maintenance", "%s is classified %s: an older release cannot run against it, so it "
                                     "is not applied automatically — a maintenance window or a forward fix is required" % (step["id"], d["class"])))
        elif any(op.split(".")[-1] in DESTRUCTIVE for op in step["operations"]):
            problems.append(_problem("migration_evidence_contradicts", "%s is classified expand but performs %s" % (step["id"], ", ".join(step["operations"]))))
        else:
            apply.append(step["id"])
    for mid in plan.get("appliedUnknown") or []:
        d = decisions.get(mid)
        if d is None or d["class"] != "expand":
            problems.append(_problem("schema_incompatible", "the database has applied %s, which this release does not know and which is "
                                     "not a reviewed expand migration; this release cannot be shown to run against the schema" % mid))
    return apply, problems


# --- the transition -------------------------------------------------------------------------

def _outcome(stage, problems, evidence=None, **extra):
    return dict({"stage": stage, "problems": problems, "evidence": evidence}, **extra)


def promote(profile, operation, release, admission, trusted_root, clock=time.time, rehearsal=False):
    """Make ``release`` (an installed release directory) what both planes serve."""
    rid = os.path.basename(release)
    journal = Journal(profile, operation)
    try:
        with HostLock(profile["lockPath"], "backend:%s" % operation):
            problems = journal.open({"release": rid})
            if problems:
                return _outcome("refused", problems)
            try:
                return _promote_locked(profile, operation, release, rid, admission, trusted_root, clock, journal, rehearsal)
            except Exception as error:
                journal.record("refused", {"exception": type(error).__name__})
                raise
    except TimeoutError as error:
        return _outcome("refused", [_problem("host_locked", str(error))])


def _refuse(journal, problems, **extra):
    journal.record("refused", {"problems": problems})
    journal.close()
    return _outcome("refused", problems, **extra)


def _promote_locked(profile, operation, release, rid, admission, trusted_root, clock, journal, rehearsal):
    problems = hp.host_problems(profile)
    problems += ins.deadline_problems(admission["deadlineEpoch"], int(clock()))
    receipt, found = ins.verify_installed(profile, release, trusted_root, rehearsal=rehearsal)
    problems += found
    if receipt and (receipt["candidate"]["recordSha256"] != admission["recordSha256"] or receipt["commit"] != admission["commit"]):
        problems.append(_problem("admission_mismatch", "the installed release was not installed from the admitted candidate"))
    problems += identity_support(release)
    current = {}
    for plane in hp.PLANES:
        try:
            current[plane] = read_include(profile, plane)
        except OSError:
            problems.append(_problem("include_missing", "%s is absent: the %s vhost must Include it before any transition "
                                     "(the cutover moves the existing directives there)" % (include_path(profile, plane), plane)))
    if problems:
        return _refuse(journal, problems)
    previous = {plane: current[plane][1] for plane in hp.PLANES}
    if all(previous[p] == rid for p in hp.PLANES):
        evidence, problems = verify_serving(profile, {p: rid for p in hp.PLANES}, budget=15)
        if not problems:
            journal.record("unchanged", {"release": rid})
            journal.close()
            return _outcome("unchanged", [], evidence, release=rid)
    config, problems = config_gate(profile, release, trusted_root)
    decisions, found = load_decisions(trusted_root)
    problems += found
    plan = None
    if not problems:
        plan, found = probe(profile, "migrate", release, "migrations", {}, trusted_root)
        problems += found
    if problems:
        return _refuse(journal, problems, config=config, plan=plan)
    apply, problems = decide_migrations(plan, decisions)
    if problems:
        return _refuse(journal, problems, config=config, plan=plan)
    journal.record("gated", {"config": config, "plan": {"pending": [s["id"] for s in plan["pending"]], "appliedUnknown": plan["appliedUnknown"]}})
    problems = ins.deadline_problems(admission["deadlineEpoch"], int(clock()))
    if problems:
        return _refuse(journal, problems)
    if apply:
        answer, problems = probe(profile, "migrate", release, "migrate", {"expect": apply}, trusted_root)
        if problems:
            journal.record("refused", {"problems": problems, "migrate": answer})
            # Deliberately NOT closed: a failed migrate leaves the schema in a state this
            # operation cannot describe; the next transition must first establish it.
            return _outcome("refused", problems + [_problem("schema_state_unknown", "a migration failed part-way: nothing was switched and "
                                                            "the old release is still serving; resume establishes what the schema is")])
        journal.record("migrated", {"applied": answer.get("applied")})
    problems = ins.deadline_problems(admission["deadlineEpoch"], int(clock()))
    if problems:
        return _refuse(journal, problems)
    return switch(profile, operation, journal, current, {p: render_include(profile, p, rid, operation) for p in hp.PLANES},
                  {p: rid for p in hp.PLANES})


def switch(profile, operation, journal, current, new, expect):
    """Replace both includes, configtest, reload, verify; restore on any failure."""
    for plane in hp.PLANES:
        journal.keep("previous-%s.conf" % plane, current[plane][0])
    journal.record("switching", {p: {"previous": current[p][1] or "legacy", "previousSha256": hashlib.sha256(current[p][0]).hexdigest(),
                                     "next": expect[p] or "legacy"} for p in hp.PLANES})
    for plane in hp.PLANES:
        _replace(include_path(profile, plane), new[plane])
    test = apache(profile, "configtest")
    if test["status"] != 0:
        for plane in hp.PLANES:
            _replace(include_path(profile, plane), current[plane][0])
        detail = (test["stdout"] + test["stderr"])[-600:]
        journal.record("restored", {"reason": "configtest", "output": detail})
        journal.close()
        return _outcome("verification-failed", [_problem("configtest_failed", "Apache rejected the new configuration; the previous files "
                                                         "were put back before any reload: %s" % detail)], restored=True)
    reload = apache(profile, profile["apache"]["reload"])
    journal.record("switched", {"reload": profile["apache"]["reload"], "status": reload["status"]})
    evidence, problems = verify_serving(profile, expect, _budget(profile), {p: include_group(new[p]) for p in hp.PLANES}) \
        if reload["status"] == 0 else \
        (None, [_problem("reload_failed", (reload["stdout"] + reload["stderr"])[-600:])])
    if not problems:
        journal.record("verified", {"evidence": evidence})
        ins._write_json(os.path.join(profile["stateDir"], "current.json"),
                        {"operation": operation, "serving": expect, "verifiedAt": ins.now_iso()}, mode=0o600)
        journal.close()
        return _outcome("verified", [], evidence, release=expect)
    journal.record("verification-failed", {"problems": problems, "evidence": evidence})
    back, restoration = restore(profile, journal, current)
    return _outcome("restored" if not restoration else "restoration-failed", problems + restoration, evidence, restoredEvidence=back)


def restore(profile, journal, current):
    """Put the previous includes back and re-verify what they name. Returns (evidence, problems)."""
    for plane in hp.PLANES:
        _replace(include_path(profile, plane), current[plane][0])
    test = apache(profile, "configtest")
    reload = apache(profile, profile["apache"]["reload"]) if test["status"] == 0 else test
    if reload["status"] != 0:
        problems = [_problem("restoration_failed", "the previous configuration could not be reloaded: %s" % (reload["stdout"] + reload["stderr"])[-600:])]
        journal.record("restoration-failed", {"problems": problems})
        return None, problems
    expect = {p: current[p][1] for p in hp.PLANES}
    evidence, problems = verify_serving(profile, expect, _budget(profile), {p: include_group(current[p][0]) for p in hp.PLANES})
    if problems:
        journal.record("restoration-failed", {"problems": problems, "evidence": evidence})
        return evidence, [_problem("restoration_failed", "the previous state did not verify after it was restored")] + problems
    journal.record("restored", {"evidence": evidence, "note": "legacy processes are shown serving; which code they loaded cannot be "
                                "established" if None in expect.values() else None})
    journal.close()
    return evidence, []


def resume(profile, operation):
    """Settle an operation a killed process left open: establish what is serving now; if the
    journal shows a switch that never verified, verify its target, and restore the kept
    previous files if it does not. Never guesses."""
    journal = Journal(profile, operation)
    try:
        with HostLock(profile["lockPath"], "backend:resume:%s" % operation):
            doc, _ = ins.read_json(journal.active)
            if not isinstance(doc, dict) or doc.get("operation") != operation:
                return _outcome("refused", [_problem("not_open", "operation %s is not the open one" % operation)])
            stages = [e["stage"] for e in journal.entries()]
            current = {p: read_include(profile, p) for p in hp.PLANES}
            serving = {p: current[p][1] for p in hp.PLANES}
            evidence, problems = verify_serving(profile, serving, _budget(profile), {p: include_group(current[p][0]) for p in hp.PLANES})
            if "switching" in stages and not problems:
                journal.record("resumed", {"serving": serving, "evidence": evidence})
                journal.record("verified", {"evidence": evidence, "note": "verified on resume"})
                journal.close()
                return _outcome("verified", [], evidence, release=serving)
            if "switching" in stages:
                kept = os.path.join(journal.dir, operation)
                previous = {}
                for p in hp.PLANES:
                    with open(os.path.join(kept, "previous-%s.conf" % p), "rb") as fh:
                        data = fh.read()
                    match = HEADER.match(data)
                    previous[p] = (data, match.group(1).decode() if match else None)
                journal.record("verification-failed", {"problems": problems, "note": "found on resume"})
                back, restoration = restore(profile, journal, previous)
                return _outcome("restored" if not restoration else "restoration-failed", problems + restoration, back)
            if problems:
                journal.record("restoration-failed", {"problems": problems, "note": "nothing was switched and the serving state does not verify"})
                return _outcome("restoration-failed", problems, evidence)
            journal.record("resumed", {"serving": serving, "note": "nothing was switched; the serving state verifies",
                                       "schemaNote": "a migration may have run" if "migrated" in stages or "gated" in stages else None})
            journal.close()
            return _outcome("resumed", [], evidence, release=serving)
    except TimeoutError as error:
        return _outcome("refused", [_problem("host_locked", str(error))])


# --- the legacy installation: adopted once, recoverable deliberately ------------------------

def adopt_legacy(profile, operation):
    """Record the includes AS THEY ARE before the first B3 transition — the legacy in-place
    installation's directives, moved into the include files by the reviewed cutover — so that
    a deliberate legacy recovery can reinstall exactly those bytes later."""
    target = os.path.join(profile["stateDir"], "legacy")
    with HostLock(profile["lockPath"], "backend:adopt-legacy:%s" % operation):
        found = {}
        for plane in hp.PLANES:
            data, rid = read_include(profile, plane)
            if rid is not None:
                return _outcome("refused", [_problem("not_legacy", "the %s include already names release %s" % (plane, rid))])
            found[plane] = data
        if os.path.exists(target):
            return _outcome("refused", [_problem("legacy_already_adopted", "%s exists; it is never overwritten" % target)])
        os.makedirs(target, mode=0o700)
        for plane, data in found.items():
            with open(os.path.join(target, INCLUDE % plane), "wb") as fh:
                fh.write(data)
        return _outcome("adopted", [], {p: hashlib.sha256(d).hexdigest() for p, d in found.items()})


def recover_legacy(profile, operation):
    """Deliberate emergency recovery to the adopted legacy directives. Reported as legacy: the
    processes are shown serving and healthy; which code they loaded cannot be established."""
    source = os.path.join(profile["stateDir"], "legacy")
    journal = Journal(profile, operation)
    try:
        with HostLock(profile["lockPath"], "backend:recover-legacy:%s" % operation):
            problems = journal.open({"target": "legacy"})
            if problems:
                return _outcome("refused", problems)
            try:
                new = {}
                for p in hp.PLANES:
                    with open(os.path.join(source, INCLUDE % p), "rb") as fh:
                        new[p] = fh.read()
            except OSError:
                return _refuse(journal, [_problem("legacy_not_adopted", "no adopted legacy directives exist under %s" % source)])
            current = {p: read_include(profile, p) for p in hp.PLANES}
            return switch(profile, operation, journal, current, new, {p: None for p in hp.PLANES})
    except TimeoutError as error:
        return _outcome("refused", [_problem("host_locked", str(error))])


def status(profile):
    active, _ = ins.read_json(os.path.join(profile["stateDir"], "active.json"))
    current, _ = ins.read_json(os.path.join(profile["stateDir"], "current.json"))
    includes = {}
    for plane in hp.PLANES:
        try:
            includes[plane] = read_include(profile, plane)[1] or "legacy"
        except OSError:
            includes[plane] = "absent"
    return {"open": active, "lastVerified": current, "includes": includes}
