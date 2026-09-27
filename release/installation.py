"""THE IMMUTABLE INSTALLATION (D08 B3) — admit one candidate on the host, install exactly its
bytes BESIDE whatever is serving, seal the result, and state what was installed in a
completion receipt. Nothing here touches what is serving; release/transition.py switches.

    <releaseRoot>/<commit>-<digest16>/     one installed release, root-owned and read-only
        source/      the verified source archive, extracted (plus bytecode compiled there)
        venv/        created AT THIS PATH from the host's base interpreter, offline, from the
                     admitted wheels alone, then reconciled — never built elsewhere and moved
        wheelhouse/  the admitted wheels, retained so the sealed environment can be reconciled
                     against the exact bytes it was installed from (B2.5's own check), for as
                     long as the release exists
        static/      collectstatic output, generated from THIS release under disposable
                     settings; release-owned, retained with the release
        wsgi/        trusted launcher files the installer writes (not from the candidate)
        receipt.json the completion receipt, written LAST; no receipt, no installation
    <releaseRoot>/.work/<operation>/       the operation's admitted inputs (root-owned, readable)
    <releaseRoot>/.operations/<id>.json    who is constructing <id>, while it is incomplete
    <releaseRoot>/.locks/<id>.lock         one construction of <id> at a time

WHO RUNS WHAT. The admission, the sealing and the receipt are this module's own code, run
by root, reading bytes and changing ownership — never executing anything from the
candidate or its environment. Everything that executes candidate code or its installer
(venv creation, pip, reconciliation's probe, compileall, collectstatic, the bounded
startup) runs as the unprivileged PREPARATION identity, with no configuration and no
secret reachable, before the tree is sealed; the reconciliation is repeated as that
identity after sealing, when it can no longer write. Root never runs the release's own
interpreter: a ``.pth`` file in a wheel would otherwise execute as root.

THE RELEASE ID is ``<commit>-<first 16 hex of sha256(environment digest : runtime digest)>``:
the commit, the certified environment, and the launcher files (which bake in the profile's
configuration and media paths). A same-named directory with other bytes is never repaired
or overwritten; it is refused.

THE INTERPRETER IS THE HOST'S. The candidate was certified on CI's CPython 3.12.3; the
installed environment is created from the host's base interpreter — the one mod_wsgi
embeds — and admitted because it meets the lock's declared target (version, implementation,
platform, machine, glibc and every marker) and reconciles to the SAME portable environment
digest the record certifies. That is compatibility within the declared target; the receipt
says so, and never that the two interpreters are the same bytes.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time

from . import candidate as cd
from . import consumer as cs
from . import environment as ev
from . import hostprofile as hp
from . import lockfile as lf
from . import preflight as pf
from . import sourcetree as st

ADMISSION_SCHEMA = "dinify.backend.admission/1"
RECEIPT_SCHEMA = "dinify.backend.installed-release/1"
RECEIPT = "receipt.json"
TOP_LEVEL = ("receipt.json", "source", "static", "venv", "wheelhouse", "wsgi")
RUNTIME_MODULE = "dinify_release_runtime.py"
STARTUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup.py")
CONSTRUCTION_WAIT_SECONDS = 600
_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_OPERATION = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
RELEASE_ID = re.compile(r"^[0-9a-f]{40}-[0-9a-f]{16}$")
# What a virtual environment legitimately carries as links: the interpreter entries and lib64.
_VENV_LINKS = re.compile(r"^venv/(bin/python[0-9.]*|lib64)$")


def _problem(code, detail):
    return {"code": code, "detail": detail}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path, doc, mode=0o644):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8")), None
    except (OSError, ValueError) as error:
        return None, "%s: %s" % (os.path.basename(path), type(error).__name__)


# --- the admission, re-established on the host ---------------------------------------------

_ADMISSION_KEYS = {
    "schema": lambda v: v == ADMISSION_SCHEMA,
    "commit": lambda v: _SHA.match(str(v)), "tree": lambda v: _SHA.match(str(v)),
    "ciRun": lambda v: _ID.match(str(v)), "ciAttempt": lambda v: re.match(r"^[1-9][0-9]{0,3}$", str(v)),
    "candidateArtifactId": lambda v: _ID.match(str(v)), "candidateDigest": lambda v: re.match(r"^sha256:[0-9a-f]{64}$", str(v)),
    "recordSha256": lambda v: _HEX.match(str(v)), "environmentDigest": lambda v: _HEX.match(str(v)),
    "preflightArtifactId": lambda v: _ID.match(str(v)), "preflightDigest": lambda v: re.match(r"^sha256:[0-9a-f]{64}$", str(v)),
    "preflightSha256": lambda v: _HEX.match(str(v)), "outcome": lambda v: v in pf.PASSING,
    "deadline": lambda v: pf._ms(v) is not None, "deadlineEpoch": lambda v: isinstance(v, int) and v > 1_000_000_000,
    "deploymentAuthorized": lambda v: v is False,
}


def validate_admission(doc):
    """The narrow, validated values the runner's receiving check produced — and nothing
    else. ``deploymentAuthorized`` must be ``false``: the preflight is evidence, never
    permission; this operation's authority is the owner's separately authorized dispatch."""
    if not isinstance(doc, dict) or sorted(doc) != sorted(_ADMISSION_KEYS):
        return ["the admission is not exactly the %s shape" % ADMISSION_SCHEMA]
    return ["admission field %s is not valid" % k for k, ok in sorted(_ADMISSION_KEYS.items()) if not ok(doc[k])]


def admission_from_receive(admitted):
    """The runner side: ``preflight.receive``'s admitted values, as the admission document."""
    return dict({k: admitted[k] for k in ("commit", "tree", "candidateArtifactId", "candidateDigest", "recordSha256", "environmentDigest",
                                          "preflightArtifactId", "preflightDigest", "preflightSha256", "outcome", "deadline", "deadlineEpoch",
                                          "deploymentAuthorized")},
                schema=ADMISSION_SCHEMA, ciRun=admitted["ciRun"], ciAttempt=admitted["ciAttempt"])


def admit(trusted_root, admission, candidate_zip, preflight_zip, work, now, margin_minutes=pf.RECEIVING_MARGIN_MINUTES):
    """Re-establish on the host, from the transported BYTES, that they are the admitted
    candidate and preflight: digests before extraction, the B2.5 consumer verification
    against the admission's identities, the preflight bound to exactly this candidate, its
    decision reproduced under the TRUSTED policy at ``trusted_root``, and its deadline still
    ahead of the HOST's clock by the margin. Nothing from the candidate executes. Returns
    ``(state, problems)``."""
    bad = validate_admission(admission)
    if bad:
        return None, [_problem("admission_invalid", d) for d in bad]
    if os.path.lexists(work) and os.listdir(work):
        return None, [_problem("workdir_not_empty", "%s must be empty" % work)]
    candidate, result_dir = os.path.join(work, "candidate"), os.path.join(work, "preflight")
    problems = pf.unpack(candidate_zip, admission["candidateDigest"], candidate)
    problems += pf.unpack(preflight_zip, admission["preflightDigest"], result_dir)
    if problems:
        return None, problems
    expect = {"repository": cd.REPOSITORY, "commit": admission["commit"], "tree": admission["tree"], "runId": admission["ciRun"],
              "runAttempt": admission["ciAttempt"], "event": "push", "ref": cd.MAIN_REF, "workflowPath": cd.WORKFLOW_PATH,
              "artifact": pf.candidate_name(admission["ciRun"], admission["ciAttempt"]), "local": False}
    state, problems = cs.verify(candidate, expect)
    if problems:
        return None, problems
    record = state["record"]
    record_sha = cd.file_sha256(os.path.join(candidate, cd.RECORD))
    if record_sha != admission["recordSha256"] or record["environment"]["digest"] != admission["environmentDigest"]:
        return None, [_problem("admission_mismatch", "the transported record is not the admitted one")]
    try:
        with open(os.path.join(result_dir, pf.PREFLIGHT_DOC), "rb") as fh:
            doc_bytes = fh.read()
        doc = json.loads(doc_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        return None, [_problem("preflight_invalid", "%s is unreadable: %s" % (pf.PREFLIGHT_DOC, type(error).__name__))]
    if hashlib.sha256(doc_bytes).hexdigest() != admission["preflightSha256"]:
        return None, [_problem("admission_mismatch", "the transported preflight is not the admitted one")]
    if not isinstance(doc, dict) or doc.get("schema") != pf.PREFLIGHT_SCHEMA or doc.get("scope") != pf.SCOPE or doc.get("decision") != "accepted":
        return None, [_problem("preflight_not_accepted", "the transported preflight is not an accepted %s result" % pf.PREFLIGHT_SCHEMA)]
    c = doc.get("candidate") or {}
    for label, actual, wanted in (("commit", doc.get("commit"), admission["commit"]), ("tree", doc.get("tree"), admission["tree"]),
                                  ("record", c.get("recordSha256"), record_sha), ("environment", c.get("environmentDigest"), admission["environmentDigest"]),
                                  ("candidate artifact", (c.get("artifact") or {}).get("id"), admission["candidateArtifactId"]),
                                  ("candidate digest", (c.get("artifact") or {}).get("digest"), admission["candidateDigest"]),
                                  ("certifying run", ((doc.get("certification") or {}).get("run") or {}).get("id"), admission["ciRun"])):
        if actual != wanted:
            problems.append(_problem("preflight_wrong_candidate", "the preflight's %s is %r; the admitted candidate's is %r" % (label, actual, wanted)))
    if problems:
        return None, problems
    reproduced, problems = pf.reproduce(trusted_root, result_dir, doc, candidate, record)
    if problems:
        return None, problems
    limit = pf.deadline_ms(doc.get("startedAt"), (doc.get("assessment") or {}).get("recordsApplied") or [])
    if limit is None or pf._iso(limit) != doc.get("deadline") or doc.get("deadline") != admission["deadline"] or limit // 1000 != admission["deadlineEpoch"]:
        return None, [_problem("preflight_time_invalid", "the preflight's deadline is not the one its window and records give, or not the admitted one")]
    problems = deadline_problems(admission["deadlineEpoch"], now, margin_minutes)
    if problems:
        return None, problems
    return {"admission": admission, "candidate": candidate, "preflight": result_dir, "record": record, "lock": state["lock"],
            "requirements": state["requirements"], "files": state["files"], "recordSha256": record_sha,
            "outcome": reproduced["outcome"]}, []


def deadline_problems(deadline_epoch, now_epoch, margin_minutes=pf.RECEIVING_MARGIN_MINUTES):
    """The assessment's deadline, against THIS host's clock, with the margin. Checked at
    admission, again when the critical-section lock is held, and again immediately before
    the switch: a queued or slow operation never carries an expired assessment into it."""
    if not isinstance(deadline_epoch, int) or now_epoch + margin_minutes * 60 >= deadline_epoch:
        return [_problem("preflight_expired", "the admitted assessment stops authorising anything at epoch %s; with a %d-minute margin it "
                         "cannot be relied on at %d — a NEW preflight is needed, and a deadline is never extended" % (deadline_epoch, margin_minutes, now_epoch))]
    return []


# --- the trusted runtime files, written into each release by this module --------------------

RUNTIME_TEMPLATE = '''"""Generated by the Dinify Backend release installer (D08 B3) for ONE installed release.

Not part of the candidate: written by the trusted installer, sealed read-only with the
release, and bound by digest into its receipt. Each plane's launcher (``<plane>.wsgi``
beside this file) calls ``application(plane)`` once per worker process.
"""
import json
import os
import secrets
import sys
import time

CONFIG = __CONFIG__
SETTINGS = {"customer": "dinify_host_settings_customer", "admin": "dinify_host_settings_admin"}
WSGI = {"customer": "dinify_backend.wsgi", "admin": "dinify_backend.wsgi_admin"}
# The installed release this file belongs to: its OWN real location, never a pointer.
RELEASE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def load_config(plane):
    """Read the plane's configuration file (outside every release) with python-decouple's own
    .env reader, so each value parses exactly as a .env found by the settings would, and
    publish it to os.environ, where decouple looks first. A variable already in the process
    environment still wins, as it does over a .env file. No release directory, and nothing
    above the release root, holds an environment file for decouple's upward search to find."""
    from decouple import RepositoryEnv
    for key, value in RepositoryEnv(CONFIG[plane]).data.items():
        os.environ.setdefault(key, value)
    os.environ["DJANGO_SETTINGS_MODULE"] = SETTINGS[plane]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def identity(plane):
    """What this process loaded, established from its own files: the receipt must name THIS
    directory, and the running interpreter and the imported project package must both live
    inside it. Anything else is reported as a mismatch, never relabelled."""
    reason, receipt = None, {}
    try:
        with open(os.path.join(RELEASE, "receipt.json"), "rb") as fh:
            receipt = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        reason = "receipt_unreadable"
    if reason is None and receipt.get("releaseId") != os.path.basename(RELEASE):
        reason = "receipt_names_another_release"
    if reason is None and os.path.realpath(sys.prefix) != os.path.join(RELEASE, "venv"):
        reason = "interpreter_outside_release"
    import dinify_backend
    if reason is None and not os.path.realpath(dinify_backend.__file__).startswith(os.path.join(RELEASE, "source") + os.sep):
        reason = "source_outside_release"
    try:
        import mod_wsgi
        wsgi_version, group = ".".join(str(p) for p in mod_wsgi.version), (mod_wsgi.process_group or None)
    except Exception:
        wsgi_version = group = None
    candidate = receipt.get("candidate") or {}
    release = None if reason == "receipt_unreadable" else {
        "id": receipt.get("releaseId"), "commit": receipt.get("commit"), "tree": receipt.get("tree"),
        "environmentDigest": receipt.get("environmentDigest"), "recordSha256": candidate.get("recordSha256"),
        "ciRun": candidate.get("ciRun"), "ciAttempt": candidate.get("ciAttempt"), "installedAt": receipt.get("installedAt")}
    return {"plane": plane, "state": "verified" if reason is None else "mismatch", "reason": reason, "release": release,
            "process": {"instance": secrets.token_hex(16), "loadedAt": _now(), "python": sys.version.split()[0],
                        "modWsgi": wsgi_version, "processGroup": group}}


def application(plane):
    load_config(plane)
    doc = identity(plane)
    try:
        from dinify_backend import release_identity
    except ImportError:
        release_identity = None   # a release older than B3 publishes nothing; the transition refuses to promote it
    if release_identity is not None:
        release_identity.install(doc)
    return __import__(WSGI[plane], fromlist=["application"]).application
'''

LAUNCHER_TEMPLATE = '''# Generated by the Dinify Backend release installer (D08 B3). The %(plane)s plane's WSGI script.
import dinify_release_runtime
application = dinify_release_runtime.application("%(plane)s")
'''

SETTINGS_TEMPLATE = '''"""Generated by the Dinify Backend release installer (D08 B3): the %(plane)s plane's settings
for an installed release — the plane's own settings module, with the two paths an immutable
release must not own: uploaded media (persistent, outside every release) and collected static
files (this release's own static/ directory)."""
import os as _os

from %(module)s import *  # noqa: F401,F403

MEDIA_ROOT = %(media)s
STATIC_ROOT = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.realpath(__file__))), "static")
'''

SETTINGS_MODULES = {"customer": "dinify_backend.settings", "admin": "dinify_backend.settings_admin"}


def runtime_files(profile):
    """The trusted files every release carries under ``wsgi/``, as ``{name: bytes}``. They
    depend on the profile (configuration and media paths), never on the release directory."""
    config = {p: profile["planes"][p]["config"] for p in hp.PLANES}
    files = {RUNTIME_MODULE: RUNTIME_TEMPLATE.replace("__CONFIG__", json.dumps(config, sort_keys=True))}
    media = json.dumps(profile["media"]["root"].rstrip("/") + "/")
    for plane in hp.PLANES:
        files["%s.wsgi" % plane] = LAUNCHER_TEMPLATE % {"plane": plane}
        files["dinify_host_settings_%s.py" % plane] = SETTINGS_TEMPLATE % {"plane": plane, "module": SETTINGS_MODULES[plane], "media": media}
    return {k: v.encode("utf-8") for k, v in files.items()}


def runtime_digest(files):
    return hashlib.sha256("".join("%s\0%s\n" % (n, hashlib.sha256(files[n]).hexdigest()) for n in sorted(files)).encode()).hexdigest()


def release_id(commit, environment_digest, runtime):
    return "%s-%s" % (commit, hashlib.sha256(("%s:%s" % (environment_digest, runtime)).encode()).hexdigest()[:16])


# --- running as the unprivileged identities ------------------------------------------------

def run_as(profile, role, argv, cwd=None, env=None, timeout=900, stdin=None):
    """``argv`` as the profile's ``role`` identity. Root drops to it with runuser; a
    rehearsal profile whose identity is the invoking account runs directly."""
    user = profile["identities"][role]
    if os.geteuid() == 0:
        if user == "root":
            raise ValueError("refusing to run a %s step as root" % role)
        argv = ["/usr/sbin/runuser", "-u", user, "--"] + list(argv)
    elif pwd.getpwuid(os.geteuid()).pw_name != user:
        raise ValueError("cannot become %s without root" % user)
    return ev.run(argv, cwd=cwd, env=env if env is not None else ev.scrubbed_env(ev.OFFLINE), timeout=timeout, stdin=stdin)


def seal_owner(profile):
    """Who owns a sealed release: root on a live host; the invoking account only in a
    rehearsal profile that says so (an unprivileged test cannot chown)."""
    return 0 if profile.get("sealOwner", "root") == "root" else os.geteuid()


# --- preparation ----------------------------------------------------------------------------

class ConstructionLock:
    """One construction of a release id at a time, bounded. Acquired by root before any
    material for the id is created; released when the receipt is written or the attempt
    is abandoned."""

    def __init__(self, root, rid, wait=CONSTRUCTION_WAIT_SECONDS):
        self.path, self.wait, self.fd = os.path.join(root, ".locks", "%s.lock" % rid), wait, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), mode=0o755, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise TimeoutError("another operation is constructing this release") from error
                time.sleep(0.5)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _marker_path(profile, rid):
    return os.path.join(profile["releaseRoot"], ".operations", "%s.json" % rid)


def free_bytes(path):
    s = os.statvfs(path)
    return s.f_bavail * s.f_frsize


def prepare(profile, state, operation, trusted_root, work, rehearsal=False, disk_free=free_bytes):
    """Install the admitted candidate as a new sealed release, or confirm an existing
    complete one is exactly it. Serving files are never read or written. Returns
    ``(receipt, problems)``; on failure only material attributable to THIS operation is
    removed."""
    if not _OPERATION.match(str(operation)):
        return None, [_problem("operation_invalid", "operation ids are lowercase words and digits")]
    record = state["record"]
    runtime = runtime_files(profile)
    rid = release_id(record["commit"], record["environment"]["digest"], runtime_digest(runtime))
    root = profile["releaseRoot"]
    release = os.path.join(root, rid)
    try:
        with ConstructionLock(root, rid):
            if os.path.lexists(os.path.join(release, RECEIPT)):
                receipt, problems = verify_installed(profile, release, trusted_root, rehearsal=rehearsal)
                if problems:
                    return None, [_problem("installed_release_mismatch", "an installed release named %s exists and is not intact: %s — it is "
                                           "refused, never repaired or overwritten" % (rid, "; ".join(p["detail"] for p in problems)[:600]))]
                if receipt["candidate"]["recordSha256"] != state["recordSha256"]:
                    return None, [_problem("installed_release_mismatch", "%s was installed from another record" % rid)]
                return dict(receipt, reused=True), []
            marker, _ = read_json(_marker_path(profile, rid))
            if os.path.lexists(release):
                if not marker or marker.get("operation") != operation:
                    return None, [_problem("partial_release_foreign", "%s exists with no completion receipt and was not started by this operation "
                                           "(%s); it is neither reused nor removed — an operator inspects it" % (rid, (marker or {}).get("operation")))]
                _remove_attributable(release)
            available = disk_free(root)
            if available < profile["minFreeBytes"]:
                return None, [_problem("insufficient_space", "%d bytes are free under the release root; installing a release needs at least %d. "
                                       "Nothing was created; already-installed releases remain available for recovery" % (available, profile["minFreeBytes"]))]
            os.makedirs(os.path.dirname(_marker_path(profile, rid)), mode=0o755, exist_ok=True)
            _write_json(_marker_path(profile, rid), {"operation": operation, "startedAt": now_iso(), "releaseId": rid})
            try:
                problems = _construct(profile, state, operation, trusted_root, work, release, rid, runtime, rehearsal)
            except BaseException:
                # Whatever went wrong, what THIS operation started is removed; nothing else is.
                _remove_attributable(release)
                os.remove(_marker_path(profile, rid))
                raise
            if problems:
                _remove_attributable(release)
                os.remove(_marker_path(profile, rid))
                return None, problems
            os.remove(_marker_path(profile, rid))
            receipt, _ = read_json(os.path.join(release, RECEIPT))
            return dict(receipt, reused=False), []
    except TimeoutError as error:
        return None, [_problem("construction_locked", str(error))]


def _remove_attributable(release):
    """Remove a partial release the CURRENT operation started (its marker names it)."""
    if os.path.lexists(release):
        for dirpath, dirnames, filenames in os.walk(release):
            os.chmod(dirpath, 0o755)
        shutil.rmtree(release)


def _construct(profile, state, operation, trusted_root, work, release, rid, runtime, rehearsal):
    prep = profile["identities"]["prepare"]
    owner = seal_owner(profile)
    build = os.path.join(work, "build")
    os.makedirs(release, mode=0o755)
    os.makedirs(build, mode=0o755)
    if os.geteuid() == 0:
        pw = pwd.getpwnam(prep)
        os.chown(release, pw.pw_uid, pw.pw_gid)
        os.chown(build, pw.pw_uid, pw.pw_gid)
    for dirpath, dirnames, filenames in os.walk(state["candidate"]):   # readable, never writable, by the preparer
        os.chmod(dirpath, 0o755)
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), 0o644)
    args = [profile["basePython"], "-E", "-s", "-B", "-m", "release", "host", "build", "--candidate", state["candidate"],
            "--release", release, "--work", build, "--expect-tree", state["record"]["tree"],
            "--base-python", profile["basePython"]]
    step = run_as(profile, "prepare", args, cwd=trusted_root, env=ev.scrubbed_env(dict(ev.OFFLINE, HOME=build)), timeout=1800)
    try:
        report = json.loads(step["stdout"])
    except ValueError:
        report = {"problems": [_problem("build_failed", "the preparation worker answered no report (status %s): %s" % (step["status"], step["stderr"][-800:]))]}
    if step["status"] != 0 or report.get("problems"):
        return report.get("problems") or [_problem("build_failed", step["stderr"][-800:])]
    if report.get("environmentDigest") != state["record"]["environment"]["digest"]:
        return [_problem("environment_mismatch", "the installed environment is not the certified one")]
    # SEAL: root-owned (or the rehearsal's own account), nothing writable by group or others,
    # nothing writable by the preparer or the runtime identity. Links only where a venv has them.
    names = sorted(os.listdir(release))
    if names != ["source", "static", "venv", "wheelhouse"]:
        return [_problem("build_unexpected", "the preparer left %s in the release; exactly source, static, venv and wheelhouse are expected" % names)]
    problems = _seal(release, owner)
    if problems:
        return problems
    wsgi = os.path.join(release, "wsgi")
    os.mkdir(wsgi, 0o755)
    for name, data in sorted(runtime.items()):
        fd = os.open(os.path.join(wsgi, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    if os.geteuid() == 0:
        for p in [wsgi] + [os.path.join(wsgi, n) for n in runtime]:
            os.chown(p, owner, owner)
    # Re-verified by the preparer AFTER sealing, when it can no longer write, so the
    # environment the receipt describes is the sealed one.
    inventory, problems = reconcile_sealed(profile, release, trusted_root)
    if problems:
        return problems
    if inventory["digest"] != state["record"]["environment"]["digest"] or inventory.get("wheelhouseDigest") != state["record"]["wheelhouse"]["digest"]:
        return [_problem("environment_mismatch", "the sealed environment or its retained wheels are not the certified ones")]
    source_files = read_tree(os.path.join(release, "source"), skip_bytecode=True)
    if st.archive_tree(source_files) != state["record"]["tree"]:
        return [_problem("source_mismatch", "the installed source does not hash to the certified tree")]
    static_listing = listing(os.path.join(release, "static"))
    admission = state["admission"]
    receipt = {
        "schema": RECEIPT_SCHEMA, "releaseId": rid, "commit": state["record"]["commit"], "tree": state["record"]["tree"],
        "environmentDigest": inventory["digest"], "installedAt": now_iso(), "operation": operation,
        "candidate": {"recordSha256": state["recordSha256"], "artifactId": admission["candidateArtifactId"], "digest": admission["candidateDigest"],
                      "ciRun": admission["ciRun"], "ciAttempt": admission["ciAttempt"], "wheelhouseDigest": state["record"]["wheelhouse"]["digest"],
                      "lockSha256": state["record"]["inputs"]["lock"]["sha256"]},
        "preflight": {"artifactId": admission["preflightArtifactId"], "digest": admission["preflightDigest"], "sha256": admission["preflightSha256"],
                      "outcome": state["outcome"], "deadline": admission["deadline"]},
        "source": {"contentSha256": st.listing_digest(source_files), "bytecodeDigest": digest_listing(listing(os.path.join(release, "source"), only_bytecode=True))},
        "static": {"digest": digest_listing(static_listing), "files": len(static_listing), "generatedFrom": rid,
                   "note": "collected from this release's own source and environment under disposable settings; not part of the certified source"},
        "runtime": {"digest": runtime_digest(runtime), "files": sorted(runtime)},
        "interpreter": {"basePython": profile["basePython"], "facts": {k: inventory["facts"].get(k) for k in ("python", "implementation", "platform", "machine", "libc", "soabi")},
                        "certifiedTarget": {k: state["record"]["target"].get(k) for k in ("python", "implementation", "platform", "machine", "libc")},
                        "statement": "the environment was created from this host's base interpreter and admitted because it meets the lock's "
                                     "declared target and reconciles to the certified portable environment digest; this is not a claim that "
                                     "the host interpreter or operating system is byte-identical to the certifying run's"},
        "profileSha256": profile.get("_sha256"),
    }
    path = os.path.join(release, RECEIPT)
    _write_json(path, receipt)
    if os.geteuid() == 0:
        os.chown(path, owner, owner)
    os.chmod(path, 0o444 if owner != 0 else 0o644)
    if owner != 0:
        os.chmod(release, 0o555)
    return []


def _seal(release, owner):
    for dirpath, dirnames, filenames in os.walk(release):
        rel_dir = os.path.relpath(dirpath, release)
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            st_ = os.lstat(full)
            if stat.S_ISLNK(st_.st_mode):
                if not _VENV_LINKS.match(rel):
                    return [_problem("build_unexpected", "%s is a link; only a virtual environment's interpreter links are expected" % rel)]
                if os.geteuid() == 0:
                    os.lchown(full, owner, owner)
                continue
            if not (stat.S_ISDIR(st_.st_mode) or stat.S_ISREG(st_.st_mode)):
                return [_problem("build_unexpected", "%s is a special file" % rel)]
            if os.geteuid() == 0:
                os.chown(full, owner, owner)
            if stat.S_ISDIR(st_.st_mode):
                os.chmod(full, 0o755 if owner == 0 else 0o555)
            else:
                executable = bool(st_.st_mode & 0o100)
                os.chmod(full, (0o755 if executable else 0o644) if owner == 0 else (0o555 if executable else 0o444))
    if os.geteuid() == 0:
        os.chown(release, owner, owner)
    os.chmod(release, 0o755)
    return []


def read_tree(root, skip_bytecode=False):
    """``{posix path: (git mode, bytes)}`` of the regular files under ``root``."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_bytecode:
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                files[rel] = ("100755" if os.lstat(full).st_mode & 0o100 else "100644", fh.read())
    return files


def listing(root, only_bytecode=False):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if only_bytecode and "/__pycache__/" not in "/" + rel:
                continue
            out.append({"filename": rel, "sha256": hp.sha256_file(full), "size": os.path.getsize(full)})
    return sorted(out, key=lambda e: e["filename"])


def digest_listing(entries):
    return cd.listing_digest(entries)


def reconcile_sealed(profile, release, trusted_root):
    """The reconciliation, as the unprivileged preparer, of a sealed release."""
    step = run_as(profile, "prepare", [profile["basePython"], "-E", "-s", "-B", "-m", "release", "host", "reconcile", "--release", release],
                  cwd=trusted_root, env=ev.scrubbed_env(ev.OFFLINE), timeout=900)
    try:
        answer = json.loads(step["stdout"])
    except ValueError:
        return None, [_problem("reconcile_failed", "no answer (status %s): %s" % (step["status"], step["stderr"][-600:]))]
    if step["status"] != 0 or answer.get("problems"):
        return None, answer.get("problems") or [_problem("reconcile_failed", step["stderr"][-600:])]
    return answer["inventory"], []


def verify_installed(profile, release, trusted_root, rehearsal=False):
    """A COMPLETE installed release is exactly what its receipt says: the receipt names this
    directory, ownership and modes are sealed, the source hashes to the certified tree, the
    static and launcher files are the recorded ones, and the environment reconciles (as the
    unprivileged preparer) to the recorded digest. Directory existence proves nothing.
    Returns ``(receipt, problems)``; nothing is repaired."""
    receipt, err = read_json(os.path.join(release, RECEIPT))
    if err or not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return None, [_problem("receipt_invalid", err or "not a %s receipt" % RECEIPT_SCHEMA)]
    rid = os.path.basename(release)
    problems = []
    if receipt.get("releaseId") != rid or not RELEASE_ID.match(rid):
        problems.append(_problem("receipt_invalid", "the receipt names %r, not this directory" % receipt.get("releaseId")))
    if sorted(os.listdir(release)) != sorted(TOP_LEVEL):
        problems.append(_problem("release_altered", "the release holds %s" % sorted(os.listdir(release))))
        return None, problems
    owner = seal_owner(profile)
    for dirpath, dirnames, filenames in os.walk(release):
        for name in [dirpath] + [os.path.join(dirpath, n) for n in dirnames + filenames]:
            st_ = os.lstat(name)
            rel = os.path.relpath(name, release)
            if stat.S_ISLNK(st_.st_mode):
                # A link's own mode is always 0777 and means nothing; its owner and target do.
                if not _VENV_LINKS.match(rel) or st_.st_uid != owner:
                    problems.append(_problem("release_altered", "%s is an unexpected link" % rel))
            elif st_.st_uid != owner or st_.st_mode & 0o022:
                problems.append(_problem("release_unsealed", "%s is owned by uid %d with mode %o" % (rel, st_.st_uid, stat.S_IMODE(st_.st_mode))))
        if len(problems) > 20:
            break
    if problems:
        return None, problems
    source_files = read_tree(os.path.join(release, "source"), skip_bytecode=True)
    if st.archive_tree(source_files) != receipt.get("tree") or st.listing_digest(source_files) != (receipt.get("source") or {}).get("contentSha256"):
        problems.append(_problem("release_altered", "the installed source no longer hashes to the certified tree"))
    if digest_listing(listing(os.path.join(release, "source"), only_bytecode=True)) != (receipt.get("source") or {}).get("bytecodeDigest"):
        problems.append(_problem("release_altered", "the installed bytecode is not what was compiled at installation"))
    if digest_listing(listing(os.path.join(release, "static"))) != (receipt.get("static") or {}).get("digest"):
        problems.append(_problem("release_altered", "the static files are not the ones generated at installation"))
    wsgi = read_tree(os.path.join(release, "wsgi"))
    if sorted(wsgi) != sorted((receipt.get("runtime") or {}).get("files") or []) or \
            runtime_digest({k: v[1] for k, v in wsgi.items()}) != (receipt.get("runtime") or {}).get("digest"):
        problems.append(_problem("release_altered", "the launcher files are not the ones written at installation"))
    if problems:
        return None, problems
    inventory, problems = reconcile_sealed(profile, release, trusted_root)
    if problems:
        return None, problems
    if inventory["digest"] != receipt.get("environmentDigest") or inventory.get("wheelhouseDigest") != (receipt.get("candidate") or {}).get("wheelhouseDigest"):
        return None, [_problem("release_altered", "the environment reconciles to %s over wheels %s; the receipt says %s over %s"
                               % (inventory["digest"], inventory.get("wheelhouseDigest"), receipt.get("environmentDigest"),
                                  (receipt.get("candidate") or {}).get("wheelhouseDigest")))]
    return receipt, []


# --- the unprivileged worker (``python -m release host build|reconcile``) --------------------

BUILD_SETTINGS = '''"""Written for ONE release build; never part of any release. Collects static files under the
repository's own disposable settings: no secret, no database, no network."""
import os as _os
from dinify_backend.test_settings import *  # noqa: F401,F403
STATIC_ROOT = _os.environ["DINIFY_BUILD_STATIC_ROOT"]
'''


def build(candidate, release, work, expect_tree, base_python):
    """As the PREPARATION identity: extract the verified source, create the environment AT
    its final path from the admitted wheels alone, reconcile it, compile bytecode, collect
    static files, and start both planes once under disposable settings. Returns a report."""
    report = {"problems": []}
    files, problems = st.read_archive(os.path.join(candidate, cd.SOURCE))
    if problems or st.archive_tree(files) != expect_tree:
        return dict(report, problems=problems or [_problem("source_mismatch", "the source archive does not hash to %s" % expect_tree)])
    with open(os.path.join(candidate, cd.RECORD), "r", encoding="utf-8") as fh:
        record = json.load(fh)
    lock, problems = lf.check(files[lf.LOCK_PATH][1], files[lf.REQUIREMENTS_PATH][1])
    if problems:
        return dict(report, problems=problems)
    found = hp.env_files_above(release)
    if found:
        return dict(report, problems=[_problem("environment_file_nearby", "an environment file sits above the release (%s)" % ", ".join(found))])
    source, venv, static = os.path.join(release, "source"), os.path.join(release, "venv"), os.path.join(release, "static")
    st.extract(files, source)
    wheelhouse = os.path.join(release, "wheelhouse")
    os.makedirs(wheelhouse)
    for entry in lf.entries(lock):
        shutil.copyfile(os.path.join(candidate, cd.WHEELHOUSE, entry["filename"]), os.path.join(wheelhouse, entry["filename"]))
    listing_, problems = ev.verify_wheelhouse(lock, wheelhouse)
    if problems or cd.listing_digest(listing_) != record["wheelhouse"]["digest"]:
        return dict(report, problems=problems or [_problem("wheelhouse_mismatch", "the copied wheels are not the certified wheelhouse")])
    install, problems = ev.create_environment(lock, wheelhouse, venv, os.path.join(work, "install"), base_python=base_python)
    report["install"] = [{k: s[k] for k in ("name", "status", "seconds")} for s in install.get("steps", [])]
    if problems:
        return dict(report, problems=problems)
    pins, _ = lf.parse_direct_inputs(files[lf.REQUIREMENTS_PATH][1].decode("utf-8"))
    inventory, problems = ev.reconcile(lock, wheelhouse, venv, pins)
    if inventory is None or problems:
        return dict(report, problems=problems)
    problems = ev.target_problems(lock, inventory["facts"])
    if inventory["digest"] != record["environment"]["digest"]:
        problems.append(_problem("environment_mismatch", "the environment built here reconciles to %s; the record certifies %s"
                                 % (inventory["digest"], record["environment"]["digest"])))
    if problems:
        return dict(report, problems=problems)
    report["environmentDigest"] = inventory["digest"]
    python = ev.venv_python(venv)
    step = ev.run([python, "-I", "-m", "compileall", "-q", source], env=ev.scrubbed_env(dict(ev.OFFLINE, PYTHONDONTWRITEBYTECODE="")), timeout=900)
    if step["status"] != 0:
        return dict(report, problems=[_problem("compile_failed", step["stdout"][-400:] + step["stderr"][-400:])])
    settings_dir = os.path.join(work, "settings")
    os.makedirs(settings_dir, exist_ok=True)
    with open(os.path.join(settings_dir, "dinify_build_settings.py"), "w", encoding="utf-8") as fh:
        fh.write(BUILD_SETTINGS)
    os.makedirs(static)
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    env = ev.scrubbed_env(dict(ev.OFFLINE, HOME=home, DJANGO_SETTINGS_MODULE="dinify_build_settings", DINIFY_BUILD_STATIC_ROOT=static,
                               PYTHONPATH=os.pathsep.join([source, settings_dir])))
    env["DJANGO_SETTINGS_MODULE"], env["PYTHONPATH"] = "dinify_build_settings", os.pathsep.join([source, settings_dir])
    step = ev.run([python, "-E", "-s", "-c", "import os,sys;sys.path[:0]=os.environ['DINIFY_PATH'].split(os.pathsep);"
                   "from django.core.management import execute_from_command_line;"
                   "execute_from_command_line(['manage.py','collectstatic','--noinput','--verbosity','0'])"],
                  cwd=home, env=dict(env, DINIFY_PATH=os.pathsep.join([source, settings_dir])), timeout=900)
    if step["status"] != 0:
        return dict(report, problems=[_problem("static_failed", "collectstatic failed: %s" % step["stderr"][-800:])])
    scratch = os.path.join(work, "startup")
    os.makedirs(os.path.join(scratch, "home"))
    imports = cs.top_level_imports(lock, wheelhouse)
    step = ev.run([python, "-I", STARTUP, "--source", source, "--scratch", scratch, "--imports", ",".join(imports)],
                  cwd=scratch, env=ev.scrubbed_env(dict(ev.OFFLINE, HOME=os.path.join(scratch, "home"))), timeout=600)
    try:
        answer = json.loads(step["stdout"])
    except ValueError:
        answer = None
    if step["status"] != 0 or not answer or not answer.get("ok"):
        return dict(report, problems=[_problem("startup_failed", "the application did not start from the installed environment: %s"
                                               % (json.dumps((answer or {}).get("failures"))[:800] if answer else step["stderr"][-800:]))])
    report["startup"] = {p: {"ok": v.get("ok")} for p, v in (answer.get("planes") or {}).items()}
    return report


def reconcile_worker(release):
    """As the preparer, against a sealed release: B2.5's own reconciliation (every installed
    file against the retained wheel it came from, the markers, pip check), after the retained
    wheels are shown to be exactly the locked ones, from the source's own lock."""
    source = os.path.join(release, "source")
    with open(os.path.join(source, lf.LOCK_PATH), "rb") as fh:
        lock_bytes = fh.read()
    with open(os.path.join(source, lf.REQUIREMENTS_PATH), "rb") as fh:
        requirements = fh.read()
    lock, problems = lf.check(lock_bytes, requirements)
    if problems:
        return {"problems": problems}
    wheelhouse = os.path.join(release, "wheelhouse")
    listing_, problems = ev.verify_wheelhouse(lock, wheelhouse)
    if problems:
        return {"problems": problems}
    pins, _ = lf.parse_direct_inputs(requirements.decode("utf-8"))
    inventory, problems = ev.reconcile(lock, wheelhouse, os.path.join(release, "venv"), pins)
    if inventory is not None:
        inventory["wheelhouseDigest"] = cd.listing_digest(listing_)
    return {"problems": problems, "inventory": inventory}
