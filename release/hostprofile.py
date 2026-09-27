"""THE INSTALLATION PROFILE (D08 B3) — the host facts the installer and the transition act on,
stated as data and validated before anything is touched.

A path or a version string appearing in this repository is not an observation of the real
machine. The profile therefore carries its own STATUS:

    unverified   written from source and reasoning; one or more fields are ``null``
                 (unknown). Every mutating command refuses it, naming each unknown field.
    verified     every field set from a reviewed, sanitized read-only discovery run
                 (``release/staged/discover_host.py``), with that observation named in
                 ``observation``. Still not authority: the owner's cutover approval is a
                 separate, human step and nothing in a profile supplies it.

And its KIND:

    live         the real host. Distinct unprivileged preparation and runtime identities,
                 neither root; nothing may be omitted.
    rehearsal    a disposable machine. Accepted ONLY by commands given ``--rehearsal``,
                 which the staged host template never passes; a rehearsal profile can
                 therefore never drive the real path, and a live one never needs the flag.

VALIDATION IS STRUCTURAL HERE (``validate``) and OBSERVED at operation time
(``host_problems``): ownership and modes of the roots, the base interpreter's facts, the
mod_wsgi module and the libpython it embeds, and that no environment file sits anywhere
python-decouple's upward search could find it from a release.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess

SCHEMA = "dinify.backend.host-profile/1"
PLANES = ("customer", "admin")
PLANE_PATHS = {  # the app's own route prefixes under each plane's Apache mount
    "customer": {"identity": "/api/v1/release/", "health": "/api/v1/health/"},
    "admin": {"identity": "/admin/v1/release/", "health": "/admin/v1/health/"},
}
_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_GROUP = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
_MOUNT = re.compile(r"^(/[A-Za-z0-9._~-]+)+$|^/$")
_IPV4 = re.compile(r"^127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")


def _problem(code, detail):
    return {"code": code, "detail": detail}


def load(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        doc = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as error:
        return None, None, [_problem("profile_unreadable", "%s: %s" % (path, type(error).__name__))]
    return doc, hashlib.sha256(data).hexdigest(), []


def _abs(value):
    return isinstance(value, str) and value.startswith("/") and os.path.normpath(value) == value and "\0" not in value \
        and not any(c in value for c in " \t\n\"'<>$`\\")


def validate(doc, rehearsal=False):
    """Structural validation. Returns problems; unknown (``null``) fields are each named."""
    problems, unknown = [], []

    def need(path, value, ok, what):
        if value is None:
            unknown.append(path)
        elif not ok(value):
            problems.append(_problem("profile_invalid", "%s is not %s" % (path, what)))

    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return [_problem("profile_invalid", "not a %s document" % SCHEMA)]
    kind, status = doc.get("kind"), doc.get("status")
    if kind not in ("live", "rehearsal"):
        problems.append(_problem("profile_invalid", "kind must be live or rehearsal"))
    if status not in ("verified", "unverified"):
        problems.append(_problem("profile_invalid", "status must be verified or unverified"))
    if kind == "rehearsal" and not rehearsal:
        problems.append(_problem("profile_rehearsal_only", "a rehearsal profile drives only commands given --rehearsal"))
    if kind == "live" and rehearsal:
        problems.append(_problem("profile_invalid", "--rehearsal was given with a live profile"))
    for key in ("releaseRoot", "stateDir", "lockPath", "basePython"):
        need(key, doc.get(key), _abs, "an absolute, normal, shell-safe path")
    interp = doc.get("interpreter") or {}
    need("interpreter.python", interp.get("python"), lambda v: re.match(r"^3\.[0-9]+\.[0-9]+$", str(v)), "a Python version")
    need("interpreter.soabi", interp.get("soabi"), lambda v: re.match(r"^[a-z0-9_-]{3,64}$", str(v)), "an SOABI")
    need("interpreter.libpython", interp.get("libpython"), _abs, "an absolute path")
    need("interpreter.libpythonSha256", interp.get("libpythonSha256"), lambda v: _HEX.match(str(v)), "a sha256")
    mw = doc.get("modWsgi") or {}
    need("modWsgi.module", mw.get("module"), _abs, "an absolute path")
    need("modWsgi.sha256", mw.get("sha256"), lambda v: _HEX.match(str(v)), "a sha256")
    need("modWsgi.version", mw.get("version"), lambda v: re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", str(v)), "a version")
    ids = doc.get("identities") or {}
    for key in ("prepare", "runtime", "runtimeGroup", "migrate"):
        need("identities.%s" % key, ids.get(key), lambda v: (_GROUP if key == "runtimeGroup" else _NAME).match(str(v)), "an account name")
    if kind == "live" and all(ids.get(k) for k in ("prepare", "runtime", "migrate")):
        if "root" in (ids["prepare"], ids["runtime"], ids["migrate"]):
            problems.append(_problem("profile_invalid", "no preparation, runtime or migration identity may be root"))
        if ids["prepare"] in (ids["runtime"], ids["migrate"]):
            problems.append(_problem("profile_invalid", "the preparation identity must not be the runtime or migration identity: "
                                     "the code it installs must not be writable by the process that serves it"))
    ap = doc.get("apache") or {}
    need("apache.ctl", ap.get("ctl"), _abs, "an absolute path")
    need("apache.includeDir", ap.get("includeDir"), _abs, "an absolute path")
    need("apache.reload", ap.get("reload"), lambda v: v in ("graceful", "restart"), "graceful or restart")
    need("apache.drainSeconds", ap.get("drainSeconds"), lambda v: isinstance(v, int) and 5 <= v <= 600, "5..600 seconds")
    planes = doc.get("planes") or {}
    if sorted(planes) != sorted(PLANES):
        problems.append(_problem("profile_invalid", "planes must be exactly %s" % ", ".join(PLANES)))
    daemons = set()
    for plane in PLANES:
        p = planes.get(plane) or {}
        base = "planes.%s." % plane
        need(base + "daemon", p.get("daemon"), lambda v: re.match(r"^[a-z][a-z0-9-]{2,40}$", str(v)), "a daemon group name")
        need(base + "mount", p.get("mount"), lambda v: _MOUNT.match(str(v)), "a URL mount path")
        need(base + "config", p.get("config"), _abs, "an absolute path")
        need(base + "processes", p.get("processes"), lambda v: isinstance(v, int) and 1 <= v <= 32, "1..32")
        need(base + "threads", p.get("threads"), lambda v: isinstance(v, int) and 1 <= v <= 64, "1..64")
        need(base + "shutdownTimeout", p.get("shutdownTimeout"), lambda v: isinstance(v, int) and 1 <= v <= 300, "1..300 seconds")
        need(base + "passAuthorization", p.get("passAuthorization"), lambda v: isinstance(v, bool), "a boolean")
        # false = this Apache does not serve the plane's static files; null = not yet observed.
        need(base + "staticUrl", p.get("staticUrl"), lambda v: v is False or (isinstance(v, str) and _MOUNT.match(v.rstrip("/"))),
             "a URL path, or false (static files not served by this Apache)")
        probe = p.get("probe") or {}
        need(base + "probe.base", probe.get("base"), lambda v: _URL.match(str(v)), "an http(s) URL")
        need(base + "probe.connectTo", probe.get("connectTo"), lambda v: _IPV4.match(str(v)), "a loopback address")
        if p.get("daemon"):
            if p["daemon"] in daemons:
                problems.append(_problem("profile_invalid", "the two planes need distinct daemon groups"))
            daemons.add(p["daemon"])
    media = doc.get("media") or {}
    need("media.root", media.get("root"), _abs, "an absolute path")
    need("media.url", media.get("url"), lambda v: v is False or (isinstance(v, str) and _MOUNT.match(v.rstrip("/"))),
         "a URL path, or false (media not served by this Apache)")
    otp = doc.get("otp") or {}
    need("otp.deterministicTestOtpAllowed", otp.get("deterministicTestOtpAllowed"), lambda v: isinstance(v, bool), "a boolean")
    if otp.get("deterministicTestOtpAllowed") is True and not (isinstance(otp.get("reason"), str) and len(otp["reason"].strip()) >= 20):
        problems.append(_problem("profile_invalid", "allowing the deterministic test OTP (ENV=dev) needs a stated reason"))
    need("minFreeBytes", doc.get("minFreeBytes"), lambda v: isinstance(v, int) and v >= 64 << 20, "at least 64 MiB")
    root = doc.get("releaseRoot")
    if _abs(root or "") and _abs(media.get("root") or "") and (media["root"] + "/").startswith(root.rstrip("/") + "/"):
        problems.append(_problem("profile_invalid", "the media root must be outside the release root: uploads survive every release"))
    for plane in PLANES:
        cfg = (planes.get(plane) or {}).get("config")
        if _abs(root or "") and _abs(cfg or "") and (cfg + "/").startswith(root.rstrip("/") + "/"):
            problems.append(_problem("profile_invalid", "the %s configuration must be outside the release root" % plane))
    obs = doc.get("observation")
    if status == "verified":
        if not (isinstance(obs, dict) and isinstance(obs.get("collectedAt"), str) and _HEX.match(str(obs.get("reportSha256")))
                and _HEX.match(str(obs.get("collectorSha256"))) and re.match(r"^mugak1/Dinify-Backend#[0-9]+$", str(obs.get("reviewedIn")))):
            problems.append(_problem("profile_unverified", "a verified profile names the reviewed discovery observation it was set from"))
        if unknown:
            problems.append(_problem("profile_invalid", "a verified profile has unknown fields"))
    if unknown:
        problems.append(_problem("profile_unknown", "unknown (null) fields: %s" % ", ".join(unknown)))
    if status != "verified" and kind == "live":
        problems.append(_problem("profile_unverified", "the live profile is unverified: its host facts have not been observed and reviewed"))
    return problems


def unknown_fields(doc):
    return [p["detail"].split(": ", 1)[1] for p in validate(doc, rehearsal=doc.get("kind") == "rehearsal")
            if p["code"] == "profile_unknown"]


# --- observed at operation time -------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _root_owned_not_writable(path, label, problems, mode_mask=0o022):
    try:
        st = os.lstat(path)
    except OSError:
        problems.append(_problem("host_unprepared", "%s (%s) does not exist" % (label, path)))
        return
    if stat.S_ISLNK(st.st_mode):
        problems.append(_problem("host_unsafe", "%s (%s) is a symbolic link" % (label, path)))
    elif st.st_uid != 0 or st.st_mode & mode_mask:
        problems.append(_problem("host_unsafe", "%s (%s) must be root-owned and not writable by group or others (mode %o, uid %d)"
                                 % (label, path, stat.S_IMODE(st.st_mode), st.st_uid)))


def env_files_above(path):
    found, current = [], os.path.realpath(path)
    while True:
        for name in (".env", "settings.ini"):
            if os.path.lexists(os.path.join(current, name)):
                found.append(os.path.join(current, name))
        parent = os.path.dirname(current)
        if parent == current:
            return found
        current = parent


def interpreter_facts(python):
    code = ("import json,platform,sys,sysconfig;print(json.dumps({'python':platform.python_version(),"
            "'implementation':platform.python_implementation(),'platform':sys.platform,'machine':platform.machine(),"
            "'libc':' '.join(platform.libc_ver()),'soabi':sysconfig.get_config_var('SOABI'),"
            "'ldlibrary':sysconfig.get_config_var('LDLIBRARY'),'libdir':sysconfig.get_config_var('LIBDIR'),"
            "'prefix':sys.prefix,'basePrefix':sys.base_prefix,'isVenv':sys.prefix!=sys.base_prefix}))")
    proc = subprocess.run([python, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=60, check=False,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def host_problems(doc, require_root=True):
    """What the host must be, observed now. Run by the privileged transition only; reads
    metadata and hashes files, changes nothing."""
    problems = []
    if require_root and os.geteuid() != 0:
        return [_problem("host_unprivileged", "the host transition runs as root; preparation and serving never do")]
    for key in ("prepare", "runtime", "migrate"):
        try:
            pwd.getpwnam(doc["identities"][key])
        except KeyError:
            problems.append(_problem("host_unprepared", "account %s (%s) does not exist" % (doc["identities"][key], key)))
    _root_owned_not_writable(doc["releaseRoot"], "the release root", problems)
    _root_owned_not_writable(doc["stateDir"], "the state directory", problems, mode_mask=0o077)
    _root_owned_not_writable(os.path.dirname(doc["lockPath"]), "the lock directory", problems)
    _root_owned_not_writable(doc["apache"]["includeDir"], "the Apache include directory", problems)
    found = env_files_above(doc["releaseRoot"])
    if found:
        problems.append(_problem("host_unsafe", "an environment file sits above the release root (%s): python-decouple searches upward "
                                 "from each release and would read it; configuration is loaded explicitly per plane" % ", ".join(found)))
    facts = interpreter_facts(doc["basePython"]) if os.path.exists(doc["basePython"]) else None
    want = doc["interpreter"]
    if facts is None:
        problems.append(_problem("host_interpreter", "the base interpreter %s could not be run" % doc["basePython"]))
    else:
        if facts["isVenv"]:
            problems.append(_problem("host_interpreter", "the base interpreter is itself a virtual environment"))
        for key in ("python", "soabi"):
            if facts[key] != want[key]:
                problems.append(_problem("host_interpreter", "the base interpreter's %s is %r; the profile states %r" % (key, facts[key], want[key])))
    for label, path, digest in (("libpython", want["libpython"], want["libpythonSha256"]),
                                ("mod_wsgi", doc["modWsgi"]["module"], doc["modWsgi"]["sha256"])):
        try:
            actual = sha256_file(path)
        except OSError:
            problems.append(_problem("host_runtime_changed", "%s (%s) is unreadable" % (label, path)))
            continue
        if actual != digest:
            problems.append(_problem("host_runtime_changed", "%s (%s) is not the observed file (sha256 %s): the embedded runtime "
                                     "changed since the profile was verified" % (label, path, actual)))
    return problems
