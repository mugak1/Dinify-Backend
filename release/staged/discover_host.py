"""READ-ONLY HOST DISCOVERY for the D08 B3 installation profile. NOT RUN AGAINST THE REAL HOST.

For a LATER, owner-approved operator run on the UAT instance (release/CUTOVER.md step 1), as
root, with the report written to a root-only file on the instance and never to the SSM output:

    umask 077; /usr/bin/python3 -I -B discover_host.py > <private report file>

It prints ONE JSON document (schema ``dinify.backend.host-discovery/2``) describing what
release/profiles/uat-backend.json needs, and on stderr exactly two lines: the sha256 of this
collector and of the report, the two values a verified profile's ``observation`` names.

READING IS NOT RETAINING. To decide what it may keep, it reads configuration IN MEMORY:
whole lines of every ``.env``/``settings.ini`` that python-decouple's upward search would find
from a WSGI directory (values included: a line cannot be classified without reading it),
every Apache file under CONFIG_GLOBS, the output of fixed read-only queries, and, for
Apache's own processes only, their /proc status, first argument, working directory and
mapped files. It never opens any process's environment. What it RETAINS AND EXPORTS is fixed
by this file, field by field:
  * environment files: ownership, mode and the PRESENCE of the keys in ENV_KEYS. No value is
    kept except ENV, mapped onto dev/test/prod or "other"; any other word is never printed;
  * Apache: only the directives in PARSERS, each through its own grammar (paths, URL mounts,
    host names, daemon groups and their reviewed options). A directive in
    REDACTED_DIRECTIVES (headers, rewrite and proxy rules, SetEnv, Define ...) is reported
    by name and line only. Every other directive is counted, never named;
  * commands: a fixed status (state, exit code, whether stderr was empty) and the facts that
    command's own parser accepts. Never raw output, never stderr text;
  * processes: only those whose executable IS Apache's binary (the /proc exe link, never a
    substring of a command line), as pid, ppid, user, group and role, plus a mod_wsgi
    daemon's group, working directory and mapped library directories. Never a command line;
  * anything it cannot read or parse becomes a fixed state (missing, denied, unsupported,
    unavailable, timeout, collector_error ...), never an echo and never a guess. No exception
    message is printed anywhere.

THE APPLICATION IS NOT CHANGED; THE HOST IS NOT UNTOUCHED. The collector opens nothing for
writing, sends nothing over the network, and starts, stops or reloads nothing. Its external
commands are the fixed queries in READ_ONLY_COMMANDS, READELF on the module wsgi.load names,
and INTERPRETER_CODE in each of INTERPRETERS. Running it still leaves traces it does not
control: the operator's redirection writes the report file, and SSM keeps its normal
bookkeeping (the command record, its retained output, the agent's logs on the instance). The
report holds host paths, account names and file digests: it is a private record, kept in the
custody the owner's approval names, and is not committed or published.

WHAT IT CANNOT SEE, each a gap for the reviewer to close on the host, never a value to infer:
directives in files outside CONFIG_GLOBS (``apache.includesNotScanned`` lists the ones
Apache includes), mod_macro expansions, ``${VAR}`` references (unsupported), AliasMatch and
other withheld targets, refs packed into ``.git/packed-refs``, ``settings.ini`` keys written
``KEY: value``, MEDIA_ROOT's value (release/hostprobe.py compares it at deploy), and a
module's NEEDED libraries when ``readelf`` is not installed.

Only RealHost touches the machine. Every other function receives what it reads through a
host object, which is how release/tests_discovery.py replaces the whole host with synthetic
inputs. It reports what it FOUND, not what should be: choosing the profile's values, and
deciding the cutover, is the reviewed human step that follows (PROFILE_FACTS says where each
profile field comes from).
"""

import builtins
import errno
import glob
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import sys

SCHEMA = "dinify.backend.host-discovery/2"
ENV_KEYS = ("ENV", "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "ADMIN_ALLOWED_HOSTS", "DINER_CAP_KEY", "ADMIN_SECRET_ENCRYPTION_KEY",
            "DATABASE_ENGINE", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST", "DATABASE_PORT",
            "YO_SMS_ACCOUNT_NO", "YO_SMS_PASSWORD", "EMAIL_HOST", "EMAIL_ACCOUNT", "EMAIL_PASSWORD", "OTP_HMAC_PEPPER",
            "MEDIA_ROOT", "STATIC_ROOT", "SECURE_SSL_REDIRECT")
ENV_WORDS = ("dev", "test", "prod")
READ_ONLY_COMMANDS = {
    "apache_version": ["apache2ctl", "-v"],
    "apache_vhosts": ["apache2ctl", "-S"],
    "apache_modules": ["apache2ctl", "-M"],
    "apache_includes": ["apache2ctl", "-t", "-D", "DUMP_INCLUDES"],
    "mod_wsgi_package": ["dpkg-query", "-W", "-f", "${Package} ${Version} ${Status}\\n", "libapache2-mod-wsgi-py3"],
    "python_packages": ["dpkg-query", "-W", "-f", "${Package} ${Version}\\n", "python3.12", "libpython3.12", "python3.12-venv"],
    "aws_cli": ["/usr/local/bin/aws", "--version"],
}
READELF = ["readelf", "-d"]          # followed by the one module path wsgi.load names
INTERPRETERS = ("/usr/bin/python3.12", "/usr/bin/python3")
INTERPRETER_CODE = ("import json,platform,sys,sysconfig;print(json.dumps({'python':platform.python_version(),"
                    "'implementation':platform.python_implementation(),'soabi':sysconfig.get_config_var('SOABI'),"
                    "'ldlibrary':sysconfig.get_config_var('LDLIBRARY'),'instsoname':sysconfig.get_config_var('INSTSONAME'),"
                    "'libdir':sysconfig.get_config_var('LIBDIR'),"
                    "'prefix':sys.prefix,'basePrefix':sys.base_prefix,'isVenv':sys.prefix!=sys.base_prefix,"
                    "'libc':' '.join(platform.libc_ver()),'machine':platform.machine()}))")
COMMAND_PATH = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")
COMMAND_ENV = {"PATH": ":".join(COMMAND_PATH), "LC_ALL": "C.UTF-8"}
APACHE_EXECUTABLES = ("/usr/sbin/apache2", "/usr/sbin/httpd")
CONFIG_GLOBS = ("/etc/apache2/apache2.conf", "/etc/apache2/ports.conf", "/etc/apache2/sites-enabled/*",
                "/etc/apache2/conf-enabled/*", "/etc/apache2/mods-enabled/wsgi.*", "/etc/apache2/dinify-backend/*")
WSGI_LOAD = "/etc/apache2/mods-enabled/wsgi.load"
DESCRIBED_PATHS = ("/etc/apache2/dinify-backend", "/srv", "/opt/dinify-backend-release", "/var/www", "/var/www/dinify-admin",
                   "/var/www/dinify-admin-releases", "/run/lock", "/var/lock")
DISK_PATHS = ("/", "/srv", "/opt", "/var", "/var/www", "/home", "/tmp")
ACCOUNTS = ("root", "ubuntu", "www-data", "postgres", "ssm-user")

READ_LIMIT = 1 << 20             # one configuration file, one /proc text file
MAPS_LIMIT = 8 << 20
OUTPUT_LIMIT = 1 << 20           # one command's stdout (and, separately, stderr)
COMMAND_TIMEOUT = 30
MAX_FILES, MAX_DIRECTIVES, MAX_VHOSTS, MAX_ENTRIES = 200, 2000, 200, 500
MAX_PROCESSES, MAX_MAPPED_DIRS, MAX_TREES, MAX_ENV_FILES, MAX_LOCKS, MAX_ACCOUNTS = 200, 60, 20, 20, 50, 20

# --- grammars: a retained string is one of these, or it is not retained ---------------------
_ABS_PATH = re.compile(r"/[A-Za-z0-9._+@~/-]{0,511}")
_PATH = re.compile(r"[A-Za-z0-9._+@~/-]{1,512}")
_GLOB_PATH = re.compile(r"[A-Za-z0-9._+@~/*?\[\]-]{1,512}")
_URL_PATH = re.compile(r"/[A-Za-z0-9._~/-]{0,255}")
_HOSTNAME = re.compile(r"(https?://)?[A-Za-z0-9*_][A-Za-z0-9*_.-]{0,252}(:[0-9]{1,5})?")
_ADDRESS = re.compile(r"(\*|_default_|[0-9]{1,3}(\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})"
                      r"(:([0-9]{1,5}|\*))?")
_LISTEN = re.compile(r"(([0-9]{1,3}(\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:.]{2,45}\]):)?[0-9]{1,5}")
_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\$?")
_WSGI_GROUP = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_WSGI_GROUP_REF = re.compile(r"%\{(GLOBAL|RESOURCE|SERVER|GROUP)\}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_DISPLAY_NAME = re.compile(r"%\{GROUP\}|[A-Za-z0-9_.:()-]{1,64}")
_CALLABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_LOCALE = re.compile(r"[A-Za-z0-9_.@-]{1,32}")
_UMASK = re.compile(r"0?[0-7]{3}")
_MODULE = re.compile(r"[a-z0-9_]{1,64}")
_DIRECTIVE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_SONAME = re.compile(r"[A-Za-z0-9._+-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_REF = re.compile(r"refs/[A-Za-z0-9._/-]{1,200}")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,99}")
_DEB_VERSION = re.compile(r"[0-9][A-Za-z0-9.+~:-]{0,99}")
_DPKG_STATUS = re.compile(r"(install|deinstall|purge|hold|unknown) (ok|reinstreq) [a-z-]{1,30}")
_PRETTY_NAME = re.compile(r"[A-Za-z0-9 ._()/+-]{1,80}")
_KERNEL = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_MACHINE = re.compile(r"[a-z0-9_]{1,20}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_INT = re.compile(r"[0-9]{1,7}")

INTERPRETER_FACTS = {"python": re.compile(r"3\.[0-9]{1,2}\.[0-9]{1,3}"), "implementation": re.compile(r"[A-Za-z]{1,20}"),
                     "soabi": re.compile(r"[a-z0-9_-]{3,64}"), "ldlibrary": re.compile(r"lib[A-Za-z0-9._+-]{1,60}"),
                     "instsoname": re.compile(r"lib[A-Za-z0-9._+-]{1,60}"),
                     "libdir": _ABS_PATH, "prefix": _ABS_PATH, "basePrefix": _ABS_PATH,
                     "libc": re.compile(r"[a-z]{1,10} [0-9.]{1,10}"), "machine": _MACHINE}

# Where each field of release/profiles/uat-backend.json comes from. "report": the part of this
# report it is read from (the reviewer assigns each WSGI daemon and mount to its plane);
# "decision": set by the reviewed profile change, which the noted part of the report informs;
# "gap": not observable here, and how it is established instead. release/tests_discovery.py
# pins this against the committed profile and against a synthetic report.
PROFILE_FACTS = {
    "releaseRoot": ("decision", "the cutover's layout; paths[] shows what exists there now"),
    "stateDir": ("decision", "the cutover's layout; paths[] shows what exists there now"),
    "lockPath": ("decision", "the shared host lock (HOST_LOCK_CONTRACT.md); locks[] shows existing ones"),
    "basePython": ("report", "interpreters[].path"),
    "interpreter.python": ("report", "interpreters[].facts.python"),
    "interpreter.soabi": ("report", "interpreters[].facts.soabi"),
    "interpreter.libpython": ("report", "interpreters[].libpython"),
    "interpreter.libpythonSha256": ("report", "interpreters[].libpythonSha256"),
    "modWsgi.module": ("report", "modWsgi.realpath"),
    "modWsgi.sha256": ("report", "modWsgi.sha256"),
    "modWsgi.version": ("report", "commands.mod_wsgi_package.packages[].version"),
    "identities.prepare": ("decision", "a new account (CUTOVER.md step 1); accounts shows which exist"),
    "identities.runtime": ("report", "apache.directives[].options.user"),
    "identities.runtimeGroup": ("report", "apache.directives[].options.group"),
    "identities.migrate": ("decision", "a new account (CUTOVER.md step 1); accounts shows which exist"),
    "apache.ctl": ("report", "commands.apache_version.resolved"),
    "apache.includeDir": ("decision", "the cutover's layout; paths[] shows whether it exists"),
    "apache.reload": ("decision", "transition policy"),
    "apache.drainSeconds": ("decision", "transition policy"),
    "planes.*.daemon": ("report", "apache.directives[].name"),
    "planes.*.mount": ("report", "apache.directives[].mount"),
    "planes.*.config": ("decision", "the cutover moves configuration to /etc/dinify-backend (CUTOVER.md step 4)"),
    "planes.*.processes": ("report", "apache.directives[].options.processes"),
    "planes.*.threads": ("report", "apache.directives[].options.threads"),
    "planes.*.shutdownTimeout": ("report", "apache.directives[].options.shutdown-timeout"),
    "planes.*.passAuthorization": ("report", "apache.directives[].enabled"),
    "planes.*.staticUrl": ("report", "apache.directives[].url"),
    "planes.*.probe.base": ("report", "commands.apache_vhosts.vhosts[].serverName"),
    "planes.*.probe.connectTo": ("decision", "a loopback address; apache.directives[].address (Listen) shows what Apache binds"),
    "media.root": ("report", "apache.directives[].path"),
    "media.url": ("report", "apache.directives[].url"),
    "otp.deterministicTestOtpAllowed": ("decision", "projectTrees[].decoupleWouldFind[].envWord shows dev/test/prod/other"),
    "otp.reason": ("decision", "the owner's stated reason"),
    "minFreeBytes": ("decision", "space policy; disks shows free space"),
}


# --- the machine ---------------------------------------------------------------------------

class RealHost:
    """THE ONLY CODE THAT TOUCHES THE MACHINE: each method is one read-only primitive.
    Everything else in this file receives what these return."""

    def read(self, path, limit):
        with open(path, "rb") as fh:
            return fh.read(limit + 1)

    def lstat(self, path):
        return os.lstat(path)

    def stat(self, path):
        return os.stat(path)

    def readlink(self, path):
        return os.readlink(path)

    def realpath(self, path):
        return os.path.realpath(path)

    def exists(self, path):
        return os.path.exists(path)

    def lexists(self, path):
        return os.path.lexists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def glob(self, pattern):
        return sorted(glob.glob(pattern))

    def listdir(self, path):
        return os.listdir(path)

    def sha256(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def which(self, name):
        """The absolute path ``name`` resolves to through COMMAND_PATH (or itself, when
        absolute), if it is an executable file; else None."""
        candidates = [name] if name.startswith("/") else [os.path.join(d, name) for d in COMMAND_PATH]
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def run(self, argv, timeout=COMMAND_TIMEOUT):
        """stdout and stderr are returned SEPARATELY and bounded; neither is ever exported."""
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                                  env=dict(COMMAND_ENV))
        except subprocess.TimeoutExpired:
            return {"state": "timeout", "exit": None, "stdout": "", "stderr": ""}
        except OSError:
            return {"state": "unavailable", "exit": None, "stdout": "", "stderr": ""}
        return {"state": "ok" if proc.returncode == 0 else "nonzero", "exit": proc.returncode,
                "stdout": proc.stdout[:OUTPUT_LIMIT].decode("utf-8", "replace"),
                "stderr": proc.stderr[:OUTPUT_LIMIT].decode("utf-8", "replace")}

    def statvfs(self, path):
        return os.statvfs(path)

    def users(self):
        return [(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir, p.pw_shell) for p in pwd.getpwall()]

    def groups(self):
        return [(g.gr_name, g.gr_gid, list(g.gr_mem)) for g in grp.getgrall()]

    def user_name(self, uid):
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None

    def group_name(self, gid):
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return None

    def euid(self):
        return os.geteuid()

    def kernel(self):
        return platform.release()

    def machine(self):
        return platform.machine()

    def now(self):
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collector_sha256():
    """The collector's own bytes: the one file read outside RealHost, and not a host fact."""
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --- small helpers -------------------------------------------------------------------------

def _ok(pattern, value):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _put(doc, key, value, pattern):
    """Retain ``value`` under ``key`` if it is in its grammar; otherwise record only that the
    reviewed field ``key`` was present and unsupported. ``None`` means absent: nothing."""
    if value is None:
        return
    if _ok(pattern, value) is None:
        doc.setdefault("unsupported", []).append(key)
    else:
        doc[key] = value


def _oserror(error):
    return {errno.ENOENT: "missing", errno.EACCES: "denied", errno.EPERM: "denied", errno.ENOTDIR: "notDirectory",
            errno.EISDIR: "isDirectory", errno.ELOOP: "loop"}.get(getattr(error, "errno", None), "unreadable")


def _error_type(error):
    name = type(error).__name__
    return name if getattr(builtins, name, None) is type(error) else "Exception"


def _section(fn, *args):
    """A failure inside one section becomes a fixed fact; its message is never printed."""
    try:
        return fn(*args)
    except Exception as error:
        return {"state": "collector_error", "errorType": _error_type(error)}


def _read_text(host, path, limit=READ_LIMIT):
    """``(text, None)`` or ``(None, state)``. A file over the limit is not partially parsed."""
    try:
        data = host.read(path, limit)
    except OSError as error:
        return None, _oserror(error)
    if len(data) > limit:
        return None, "tooLarge"
    return data.decode("utf-8", "replace"), None


def _lines(text):
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


def _bounded(doc, key, items, limit):
    doc[key] = items[:limit]
    if len(items) > limit:
        doc.setdefault("omitted", {})[key] = len(items) - limit


def _digest(host, doc, key, path):
    try:
        value = host.sha256(path)
    except OSError as error:
        doc[key + "State"] = _oserror(error)
        return
    _put(doc, key, value, _SHA256)


def describe(host, path):
    """Type, ownership and mode of ``path`` (and a link's target). Never contents."""
    doc = {}
    _put(doc, "path", path, _ABS_PATH)
    try:
        st = host.lstat(path)
    except OSError as error:
        return dict(doc, exists=False, state=_oserror(error))
    mode = st.st_mode
    kind = "link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"
    doc.update(exists=True, type=kind, mode="%o" % stat.S_IMODE(mode))
    owner, group = host.user_name(st.st_uid), host.group_name(st.st_gid)
    doc["owner"] = owner if _ok(_ACCOUNT, owner) else st.st_uid
    doc["group"] = group if _ok(_ACCOUNT, group) else st.st_gid
    if kind == "link":
        try:
            _put(doc, "target", host.readlink(path), _PATH)
        except OSError as error:
            doc["targetState"] = _oserror(error)
    return doc


# --- environment files ---------------------------------------------------------------------

def parse_env_text(text):
    """Which ENV_KEYS are present, and ENV's word as dev/test/prod/other. Every line is read
    in memory to decide that; no value is kept but that word."""
    present, env_word = [], None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.strip().removeprefix("export ").strip()
        if sep and key in ENV_KEYS:
            present.append(key)
            if key == "ENV":
                word = value.strip().strip("'\"")
                env_word = word if word in ENV_WORDS else "other"
        del line, value
    return {"keysPresent": sorted(set(present)), "keysAbsent": sorted(set(ENV_KEYS) - set(present)), "envWord": env_word}


def env_file(host, path):
    doc = describe(host, path)
    if not doc.get("exists") or doc.get("type") != "file":
        return doc
    text, state = _read_text(host, path)
    if text is None:
        return dict(doc, state=state)
    return dict(doc, **parse_env_text(text))


# --- Apache configuration ------------------------------------------------------------------

_INTK, _ONOFF, _PATHS = "int", "on/off", "paths"
DAEMON_OPTIONS = {
    "user": _ACCOUNT, "group": _ACCOUNT, "display-name": _DISPLAY_NAME, "home": _ABS_PATH, "python-home": _ABS_PATH,
    "python-path": _PATHS, "lang": _LOCALE, "locale": _LOCALE, "umask": _UMASK,
    **{k: _INTK for k in ("processes", "threads", "shutdown-timeout", "maximum-requests", "inactivity-timeout",
                          "request-timeout", "graceful-timeout", "eviction-timeout", "deadlock-timeout", "queue-timeout",
                          "connect-timeout", "socket-timeout", "startup-timeout", "restart-interval", "listen-backlog")},
}
SCRIPT_ALIAS_OPTIONS = {"process-group": _WSGI_GROUP, "application-group": _WSGI_GROUP_REF, "callable-object": _CALLABLE,
                        "pass-authorization": _ONOFF}


def _typed(kind, value):
    if kind == _INTK:
        return int(value) if _INT.fullmatch(value) else None
    if kind == _ONOFF:
        return {"on": True, "off": False}.get(value.lower())
    if kind == _PATHS:
        parts = value.split(":")
        return parts if all(_ABS_PATH.fullmatch(p) for p in parts) else None
    return value if kind.fullmatch(value) else None


def _options(args, schema):
    """Reviewed ``key=value`` options only. An unknown key is counted, never named; a value
    outside its grammar, or a repeated key, is unsupported."""
    options, bad, seen, unreviewed = {}, set(), set(), 0
    for token in args:
        key, sep, value = token.partition("=")
        if not sep or key not in schema:
            unreviewed += 1
            continue
        if key in seen:
            bad.add(key)
            options.pop(key, None)
            continue
        seen.add(key)
        parsed = _typed(schema[key], value)
        if parsed is None:
            bad.add(key)
        else:
            options[key] = parsed
    doc = {"options": options}
    if unreviewed:
        doc["unreviewedOptions"] = unreviewed
    if bad:
        doc["unsupported"] = ["options." + k for k in sorted(bad)]
    return doc


def _single(field, kind):
    def parse(args):
        if len(args) != 1:
            return {"unsupported": ["arguments"]}
        value = _typed(kind, args[0])
        return {field: value} if value is not None else {"unsupported": [field]}
    return parse


def _pair(first, first_kind, second, second_kind):
    def parse(args):
        if len(args) != 2:
            return {"unsupported": ["arguments"]}
        doc = {}
        for field, kind, value in ((first, first_kind, args[0]), (second, second_kind, args[1])):
            parsed = _typed(kind, value)
            if parsed is None:
                doc.setdefault("unsupported", []).append(field)
            else:
                doc[field] = parsed
        return doc
    return parse


def _wsgi_daemon(args):
    if not args:
        return {"unsupported": ["arguments"]}
    doc = _options(args[1:], DAEMON_OPTIONS)
    if _ok(_WSGI_GROUP, args[0]):
        doc["name"] = args[0]
    else:
        doc.setdefault("unsupported", []).insert(0, "name")
    return doc


def _wsgi_script_alias(args):
    if len(args) < 2:
        return {"unsupported": ["arguments"]}
    doc = _options(args[2:], SCRIPT_ALIAS_OPTIONS)
    for field, pattern, value in (("mount", _URL_PATH, args[0]), ("script", _ABS_PATH, args[1])):
        if _ok(pattern, value):
            doc[field] = value
        else:
            doc.setdefault("unsupported", []).insert(0, field)
    return doc


def _listen(args):
    if len(args) not in (1, 2):
        return {"unsupported": ["arguments"]}
    doc = {}
    _put(doc, "address", args[0], _LISTEN)
    if len(args) == 2:
        _put(doc, "protocol", args[1].lower(), re.compile(r"https?"))
    return doc


def _server_alias(args):
    names = [a for a in args if _ok(_HOSTNAME, a)]
    doc = {"names": names}
    if len(names) != len(args) or not args:
        doc["unsupported"] = ["names"]
    return doc


PARSERS = {
    "WSGIDaemonProcess": _wsgi_daemon,
    "WSGIScriptAlias": _wsgi_script_alias,
    "WSGIProcessGroup": _single("name", _WSGI_GROUP_REF),
    "WSGIApplicationGroup": _single("name", _WSGI_GROUP_REF),
    "WSGIPassAuthorization": _single("enabled", _ONOFF),
    "WSGIRestrictEmbedded": _single("enabled", _ONOFF),
    "WSGIPythonHome": _single("path", _ABS_PATH),
    "WSGIPythonPath": _single("paths", _PATHS),
    "WSGISocketPrefix": _single("path", _PATH),
    "ServerName": _single("name", _HOSTNAME),
    "ServerAlias": _server_alias,
    "Alias": _pair("url", _URL_PATH, "path", _ABS_PATH),
    "Include": _single("path", _GLOB_PATH),
    "IncludeOptional": _single("path", _GLOB_PATH),
    "DocumentRoot": _single("path", _ABS_PATH),
    "Listen": _listen,
    "LoadModule": _pair("module", _MODULE, "path", _PATH),
    "Directory": _single("path", _GLOB_PATH),
    "Location": _single("url", _URL_PATH),
}
# Named by line, never shown: each can carry a credential, a token or an internal address.
REDACTED_DIRECTIVES = ("SetEnv", "SetEnvIf", "SetEnvIfNoCase", "SetEnvIfExpr", "PassEnv", "UnsetEnv", "Define", "Header",
                       "RequestHeader", "RewriteRule", "RewriteCond", "RewriteMap", "ProxyPass", "ProxyPassMatch",
                       "ProxyPassReverse", "AliasMatch", "ScriptAlias", "Redirect", "RedirectMatch")
_CANONICAL = {name.lower(): name for name in tuple(PARSERS) + REDACTED_DIRECTIVES + ("VirtualHost",)}


def _logical_lines(text):
    """``(first physical line number, text)``, a trailing backslash joining the next line."""
    out, parts, start = [], [], None
    for number, raw in enumerate(text.split("\n"), 1):
        raw = raw.rstrip("\r")
        start = number if start is None else start
        if raw.endswith("\\"):
            parts.append(raw[:-1])
            continue
        parts.append(raw)
        out.append((start, " ".join(parts)))
        parts, start = [], None
    if parts:
        out.append((start, " ".join(parts)))
    return out


def _tokens(text):
    """Apache's argument words: whitespace-separated, a word that starts with a quote runs to
    the matching quote. None when a quote is unterminated."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        quote = text[i] if text[i] in "\"'" else None
        if quote:
            j, buf = i + 1, []
            while j < n and text[j] != quote:
                if text[j] == "\\" and j + 1 < n and text[j + 1] == quote:
                    j += 1
                buf.append(text[j])
                j += 1
            if j >= n:
                return None
            out.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace():
                j += 1
            out.append(text[i:j])
            i = j
    return out


def parse_config_text(text, path, first_vhost=0):
    """One Apache configuration file, as reviewed records only."""
    directives, vhosts, other, current = [], [], 0, None
    for line_number, line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        closing = stripped.startswith("</")
        inner = stripped
        if stripped.startswith("<"):
            inner = stripped[2:] if closing else stripped[1:]
            if not inner.endswith(">"):
                other += 1
                continue
            inner = inner[:-1]
        words = inner.split(None, 1)
        if not words:
            other += 1
            continue
        raw_name, rest = words[0], (words[1] if len(words) > 1 else "")
        name = _CANONICAL.get(raw_name.lower()) if _DIRECTIVE_NAME.fullmatch(raw_name) else None
        if closing:
            if name == "VirtualHost":
                current = None
            continue
        if name is None:
            other += 1
            continue
        args = _tokens(rest)
        if name == "VirtualHost":
            index = first_vhost + len(vhosts)
            vhost = {"index": index, "file": path, "line": line_number,
                     "addresses": [a for a in (args or []) if _ok(_ADDRESS, a)]}
            if not args or len(vhost["addresses"]) != len(args):
                vhost["unsupported"] = ["addresses"]
            vhosts.append(vhost)
            current = index
            continue
        record = {"file": path, "line": line_number, "directive": name, "vhost": current}
        if name in REDACTED_DIRECTIVES:
            record["state"] = "withheld"
        elif args is None:
            record.update(state="unsupported", unsupported=["arguments"])
        else:
            record.update(PARSERS[name](args))
            record["state"] = "unsupported" if record.get("unsupported") else "parsed"
        directives.append(record)
    return {"directives": directives, "vhosts": vhosts, "otherDirectives": other}


def apache_config(host):
    """Every file under CONFIG_GLOBS: its metadata and digest, and its reviewed directives."""
    paths = sorted({p for pattern in CONFIG_GLOBS for p in host.glob(pattern)})
    doc = {"files": [], "vhosts": [], "directives": []}
    unsupported = 0
    for path in paths[:MAX_FILES]:
        if not _ok(_ABS_PATH, path):
            unsupported += 1
            continue
        entry = describe(host, path)
        real = host.realpath(path)
        _put(entry, "realpath", real, _ABS_PATH)
        target = entry.get("realpath")
        if target:
            _digest(host, entry, "sha256", target)
            text, state = _read_text(host, target)
            if text is None:
                entry["state"] = state
            else:
                parsed = parse_config_text(text, path, len(doc["vhosts"]))
                entry["otherDirectives"] = parsed["otherDirectives"]
                doc["vhosts"] += parsed["vhosts"]
                doc["directives"] += parsed["directives"]
        doc["files"].append(entry)
    if len(paths) > MAX_FILES:
        doc.setdefault("omitted", {})["files"] = len(paths) - MAX_FILES
    if unsupported:
        doc["unsupportedFiles"] = unsupported
    directives, vhosts = doc.pop("directives"), doc.pop("vhosts")
    _bounded(doc, "directives", directives, MAX_DIRECTIVES)
    _bounded(doc, "vhosts", vhosts, MAX_VHOSTS)
    return doc


# --- commands: one parser each; raw output never leaves its parser -------------------------

def parse_apache_version(stdout, stderr):
    doc, unparsed = {}, 0
    for line in _lines(stdout):
        version = re.fullmatch(r"Server version: Apache/([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}) \(([A-Za-z]{1,20})\)", line)
        built = re.fullmatch(r"Server built:\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})", line)
        if version:
            doc.update(version=version.group(1), distribution=version.group(2))
        elif built:
            doc["built"] = built.group(1)
        else:
            unparsed += 1
    doc["unparsedLines"] = unparsed
    return doc


_VH_NAMEVHOST = re.compile(r"(\S+)\s+is a NameVirtualHost")
_VH_ONLY = re.compile(r"(\S+)\s+(\S+) \((\S+):([0-9]{1,6})\)")
_VH_DEFAULT = re.compile(r"\s+default server (\S+) \((\S+):([0-9]{1,6})\)")
_VH_PORT = re.compile(r"\s+port ([0-9]{1,5}) namevhost (\S+) \((\S+):([0-9]{1,6})\)")
_VH_ALIAS = re.compile(r"\s+(?:wild )?alias (\S+)")
_RUN_AS = re.compile(r"(User|Group): name=\"([^\"]*)\" id=([0-9]{1,10})(?: not_used)?")


def _vhost_entry(role, address, name, file, line, port=None):
    entry = {"role": role, "line": int(line)}
    _put(entry, "address", address, _ADDRESS)
    _put(entry, "serverName", name, _HOSTNAME)
    _put(entry, "file", file, _ABS_PATH)
    if port is not None:
        entry["port"] = int(port)
    return entry


def parse_apache_vhosts(stdout, stderr):
    vhosts, unparsed, address = [], 0, None
    doc = {}
    for line in _lines(stdout):
        if line == "VirtualHost configuration:":
            continue
        m = _VH_NAMEVHOST.fullmatch(line)
        if m:
            address = m.group(1)
            continue
        m = _VH_DEFAULT.fullmatch(line)
        if m:
            vhosts.append(_vhost_entry("default", address, *m.groups()))
            continue
        m = _VH_PORT.fullmatch(line)
        if m:
            vhosts.append(_vhost_entry("namevhost", address, m.group(2), m.group(3), m.group(4), port=m.group(1)))
            continue
        m = _VH_ALIAS.fullmatch(line)
        if m and vhosts:
            alias = m.group(1)
            if _ok(_HOSTNAME, alias):
                vhosts[-1].setdefault("aliases", []).append(alias)
            else:
                vhosts[-1].setdefault("unsupported", []).append("aliases")
            continue
        m = _VH_ONLY.fullmatch(line)
        if m:
            address = None
            vhosts.append(_vhost_entry("only", *m.groups()))
            continue
        m = re.fullmatch(r"(ServerRoot|Main DocumentRoot): \"([^\"]*)\"", line)
        if m:
            _put(doc, "serverRoot" if m.group(1) == "ServerRoot" else "documentRoot", m.group(2), _ABS_PATH)
            continue
        m = _RUN_AS.fullmatch(line)
        if m:
            run_as = {"id": int(m.group(3))}
            _put(run_as, "name", m.group(2), _ACCOUNT)
            doc["runUser" if m.group(1) == "User" else "runGroup"] = run_as
            continue
        unparsed += 1
    _bounded(doc, "vhosts", vhosts, MAX_VHOSTS)
    doc["unparsedLines"] = unparsed
    return doc


def parse_apache_modules(stdout, stderr):
    modules, unparsed = [], 0
    for line in _lines(stdout):
        m = re.fullmatch(r"\s*([a-z0-9_]{1,64}) \((static|shared)\)", line)
        if m:
            modules.append({"name": m.group(1), "kind": m.group(2)})
        elif line != "Loaded Modules:":
            unparsed += 1
    doc = {"unparsedLines": unparsed}
    _bounded(doc, "modules", modules, MAX_ENTRIES)
    return doc


def parse_apache_includes(stdout, stderr):
    files, unparsed = [], 0
    for line in _lines(stdout):
        m = re.fullmatch(r"\s*\((\*|[0-9]{1,6})\) (\S+)", line)
        if m and _ok(_ABS_PATH, m.group(2)):
            files.append(m.group(2))
        elif line.strip() not in ("Included configuration files:", "Syntax OK"):
            unparsed += 1
    doc = {"unparsedLines": unparsed,
           "syntaxOk": any(line.strip() == "Syntax OK" for line in _lines(stdout) + _lines(stderr))}
    _bounded(doc, "files", files, MAX_ENTRIES)
    return doc


def parse_dpkg(stdout, stderr):
    packages, unparsed = [], 0
    for line in _lines(stdout):
        m = re.fullmatch(r"(\S+) (\S*)( \S+ \S+ \S+)?", line)
        if not m or not _ok(_PACKAGE, m.group(1)):
            unparsed += 1
            continue
        entry = {"package": m.group(1), "version": None}       # null: listed, nothing installed
        if m.group(2):
            _put(entry, "version", m.group(2), _DEB_VERSION)
        if m.group(3):
            _put(entry, "status", m.group(3).strip(), _DPKG_STATUS)
        packages.append(entry)
    doc = {"unparsedLines": unparsed}
    _bounded(doc, "packages", packages, MAX_ENTRIES)
    return doc


def parse_aws_version(stdout, stderr):
    doc, unparsed = {}, 0
    for line in _lines(stdout):
        m = re.match(r"aws-cli/([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4})(\s|$)", line)
        if m and "version" not in doc:
            doc["version"] = m.group(1)
        else:
            unparsed += 1
    doc["unparsedLines"] = unparsed
    return doc


def parse_readelf_needed(stdout, stderr):
    needed, unsupported = [], 0
    for line in _lines(stdout):
        m = re.fullmatch(r"\s*0x[0-9a-fA-F]{1,16} \(NEEDED\)\s+Shared library: \[(.*)\]", line)
        if m:
            if _ok(_SONAME, m.group(1)):
                needed.append(m.group(1))
            else:
                unsupported += 1
    doc = {"needed": needed[:MAX_ENTRIES]}
    if unsupported:
        doc["unsupportedNeeded"] = unsupported
    return doc


def parse_interpreter_facts(stdout, stderr):
    try:
        facts = json.loads(stdout)
    except ValueError:
        return {"facts": None, "factsState": "unparseable"}
    if not isinstance(facts, dict):
        return {"facts": None, "factsState": "unparseable"}
    doc = {}
    for key, pattern in INTERPRETER_FACTS.items():
        _put(doc, key, facts.get(key), pattern)
    if isinstance(facts.get("isVenv"), bool):
        doc["isVenv"] = facts["isVenv"]
    elif "isVenv" in facts:
        doc.setdefault("unsupported", []).append("isVenv")
    return {"facts": doc}


COMMAND_PARSERS = {
    "apache_version": parse_apache_version,
    "apache_vhosts": parse_apache_vhosts,
    "apache_modules": parse_apache_modules,
    "apache_includes": parse_apache_includes,
    "mod_wsgi_package": parse_dpkg,
    "python_packages": parse_dpkg,
    "aws_cli": parse_aws_version,
}


def command_fact(host, argv, parser):
    """Resolve ``argv[0]``, run it, and keep only a fixed status plus what ``parser`` accepts."""
    resolved = host.which(argv[0])
    if resolved is None:
        return {"state": "unavailable"}
    if not _ok(_ABS_PATH, resolved):
        return {"state": "unsupported"}
    result = host.run([resolved] + list(argv[1:]))
    doc = {"resolved": resolved, "state": result["state"], "exit": result["exit"],
           "stderr": "present" if result["stderr"].strip() else "empty"}
    if result["state"] in ("ok", "nonzero"):
        doc.update(parser(result["stdout"], result["stderr"]))
    return doc


def commands(host):
    return {key: _section(command_fact, host, argv, COMMAND_PARSERS[key]) for key, argv in READ_ONLY_COMMANDS.items()}


# --- interpreter and mod_wsgi --------------------------------------------------------------

def interpreter(host, python):
    doc = {"path": python}
    real = host.realpath(python)
    _put(doc, "realpath", real, _ABS_PATH)
    if doc.get("realpath"):
        _digest(host, doc, "sha256", real)
    doc.update(command_fact(host, [python, "-I", "-S", "-B", "-c", INTERPRETER_CODE], parse_interpreter_facts))
    # The shared library itself is LIBDIR/INSTSONAME (what mod_wsgi's NEEDED names); LDLIBRARY's
    # unversioned link may exist only with the -dev package. Neither is guessed if absent.
    facts = doc.get("facts") or {}
    name = facts.get("instsoname") or facts.get("ldlibrary")
    if facts.get("libdir") and name:
        doc["libpythonFrom"] = "INSTSONAME" if facts.get("instsoname") else "LDLIBRARY"
        _put(doc, "libpython", host.realpath(facts["libdir"].rstrip("/") + "/" + name), _ABS_PATH)
        if doc.get("libpython"):
            _digest(host, doc, "libpythonSha256", doc["libpython"])
    return doc


def mod_wsgi(host):
    if not host.glob(WSGI_LOAD):
        return {"state": "absent"}
    doc = {"loadFile": WSGI_LOAD}
    text, state = _read_text(host, WSGI_LOAD)
    if text is None:
        return dict(doc, state=state)
    found = set()
    for _, line in _logical_lines(text):
        words = _tokens(line.strip()) or []
        if len(words) == 3 and words[0].lower() == "loadmodule" and "wsgi" in words[1]:
            found.add((words[1], words[2]))
    if len(found) != 1:
        return dict(doc, state="noLoadModule" if not found else "ambiguous")
    module, path = found.pop()
    if not (_ok(_MODULE, module) and _ok(_ABS_PATH, path)):
        return dict(doc, state="unsupported")
    doc.update(state="found", moduleName=module, module=path)
    _put(doc, "realpath", host.realpath(path), _ABS_PATH)
    if doc.get("realpath"):
        _digest(host, doc, "sha256", doc["realpath"])
        readelf = _section(command_fact, host, READELF + [doc["realpath"]], parse_readelf_needed)
        doc["neededState"] = readelf.get("state")
        if "needed" in readelf:
            doc["needed"] = readelf["needed"]
    return doc


# --- processes: Apache's binary, identified by its executable ------------------------------

def parse_proc_status(text):
    doc = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields = value.split()
        if key == "PPid" and len(fields) == 1 and _INT.fullmatch(fields[0]):
            doc["ppid"] = int(fields[0])
        elif key in ("Uid", "Gid") and len(fields) == 4 and _INT.fullmatch(fields[1]):
            doc[key.lower()] = int(fields[1])      # effective id
    return doc


def parse_wsgi_title(argv0):
    """``(group, truncated)`` for a mod_wsgi daemon title ``(wsgi:<group>)``, which mod_wsgi
    writes over argv[0] and Linux may truncate; ``None`` for any other first argument."""
    if not argv0.startswith("(wsgi:"):
        return None
    group = argv0[len("(wsgi:"):]
    truncated = not group.endswith(")")
    group = group if truncated else group[:-1]
    return (group if _ok(_WSGI_GROUP, group) else None), truncated


def _first_argument(host, pid):
    data = host.read("/proc/%s/cmdline" % pid, 4096)
    return data.split(b"\0")[0].decode("utf-8", "replace").strip()


def _mapped_dirs(host, pid):
    text, state = _read_text(host, "/proc/%s/maps" % pid, MAPS_LIMIT)
    if text is None:
        return {"mappedState": state}
    dirs, unsupported = set(), 0
    for line in text.splitlines():
        parts = line.split(None, 5)
        path = parts[5].strip() if len(parts) == 6 else ""
        if path.endswith((".so", ".py", ".pyc")) or ".so." in path:
            directory = path.rsplit("/", 1)[0] or "/"
            if _ok(_ABS_PATH, directory):
                dirs.add(directory)
            else:
                unsupported += 1
    doc = {}
    _bounded(doc, "mappedDirs", sorted(dirs), MAX_MAPPED_DIRS)
    if unsupported:
        doc["mappedUnsupported"] = unsupported
    return doc


def processes(host):
    try:
        pids = sorted((p for p in host.listdir("/proc") if p.isdigit()), key=int)
    except OSError as error:
        return {"state": _oserror(error)}
    found, unreadable = [], 0
    for pid in pids:
        try:
            exe = host.readlink("/proc/%s/exe" % pid)
        except OSError:
            unreadable += 1                      # kernel threads, races, a process gone
            continue
        deleted = exe.endswith(" (deleted)")
        exe = exe[:-len(" (deleted)")] if deleted else exe
        if exe not in APACHE_EXECUTABLES:
            continue
        entry = {"pid": int(pid), "executable": exe, "exeDeleted": deleted}
        text, state = _read_text(host, "/proc/%s/status" % pid, 65536)
        status = parse_proc_status(text) if text is not None else {}
        if text is None:
            entry["statusState"] = state
        if "ppid" in status:
            entry["ppid"] = status["ppid"]
        for key, lookup in (("uid", host.user_name), ("gid", host.group_name)):
            if key in status:
                name = lookup(status[key])
                entry["user" if key == "uid" else "group"] = name if _ok(_ACCOUNT, name) else status[key]
        try:
            title = parse_wsgi_title(_first_argument(host, pid))
        except OSError:
            entry["role"] = "unknown"
            found.append(entry)
            continue
        if title is None:
            entry["role"] = "server"
        else:
            entry.update(role="wsgi-daemon", titleTruncated=title[1])
            if title[0]:
                entry["wsgiGroup"] = title[0]
            else:
                entry["unsupported"] = ["wsgiGroup"]
            try:
                _put(entry, "cwd", host.readlink("/proc/%s/cwd" % pid), _ABS_PATH)
            except OSError as error:
                entry["cwdState"] = _oserror(error)
            entry.update(_mapped_dirs(host, pid))
        found.append(entry)
    doc = {"scanned": len(pids), "exeUnreadable": unreadable}
    _bounded(doc, "apache", found, MAX_PROCESSES)
    return doc


# --- project trees, accounts, disks, paths, locks ------------------------------------------

def _tree_paths(directives):
    """Absolute paths the parsed WSGI and Alias directives name."""
    paths = []
    for d in directives:
        name, options = d.get("directive"), d.get("options") or {}
        if name == "WSGIScriptAlias" and d.get("script"):
            paths.append(d["script"])
        elif name == "WSGIDaemonProcess":
            paths += [options[k] for k in ("home", "python-home") if k in options] + options.get("python-path", [])
        elif name == "WSGIPythonHome" and d.get("path"):
            paths.append(d["path"])
        elif name == "WSGIPythonPath":
            paths += d.get("paths", [])
        elif name == "Alias" and d.get("path"):
            paths.append(d["path"])
    return paths


def _git(host, path):
    doc = {}
    text, state = _read_text(host, path.rstrip("/") + "/.git/HEAD", 256)
    if text is None:
        return {"gitState": state}
    head = text.strip()
    if _ok(_GIT_OBJECT, head):
        return {"gitCommit": head, "gitDetached": True}
    if head.startswith("ref: ") and _ok(_GIT_REF, head[5:]) and ".." not in head:
        doc["gitRef"] = head[5:]
        ref, state = _read_text(host, path.rstrip("/") + "/.git/" + head[5:], 128)
        if ref is None:
            doc["gitCommitState"] = state
        else:
            _put(doc, "gitCommit", ref.strip(), _GIT_OBJECT)
        return doc
    return {"gitState": "unsupported"}


def project_trees(host, directives):
    """Directories the WSGI directives name: layout, ownership, the git commit checked out,
    and the environment files python-decouple's upward search would find."""
    paths = set()
    for path in _tree_paths(directives):
        path = path.rstrip("/") or "/"
        if host.exists(path):
            paths.add(path if host.isdir(path) else (path.rsplit("/", 1)[0] or "/"))
    trees = []
    for path in sorted(paths)[:MAX_TREES]:
        doc = describe(host, path)
        doc.update(_git(host, path))
        envs, current = [], host.realpath(path)
        while _ok(_ABS_PATH, current):
            for name in (".env", "settings.ini"):
                candidate = current.rstrip("/") + "/" + name
                if host.lexists(candidate):
                    envs.append(env_file(host, candidate))
            if current == "/":
                break
            current = current.rsplit("/", 1)[0] or "/"
        _bounded(doc, "decoupleWouldFind", envs, MAX_ENV_FILES)
        trees.append(doc)
    return trees


def accounts(host):
    users, groups = host.users(), host.groups()
    wanted = list(ACCOUNTS) + sorted(u[0] for u in users if _ok(_ACCOUNT, u[0]) and u[0].startswith("dinify"))
    found = {}
    for name in wanted[:len(ACCOUNTS) + MAX_ACCOUNTS]:
        match = next((u for u in users if u[0] == name), None)
        if match is None:
            found[name] = None
            continue
        doc = {"uid": match[1], "gid": match[2],
               "groups": sorted(g[0] for g in groups if name in g[2] and _ok(_ACCOUNT, g[0]))}
        _put(doc, "home", match[3], _ABS_PATH)
        _put(doc, "shell", match[4], _ABS_PATH)
        found[name] = doc
    return found


def disks(host):
    found = {}
    for path in DISK_PATHS:
        try:
            st = host.statvfs(path)
            found[path] = {"freeBytes": st.f_bavail * st.f_frsize, "totalBytes": st.f_blocks * st.f_frsize,
                           "device": host.stat(path).st_dev}
        except OSError:
            found[path] = None
    return found


def locks(host):
    found = sorted(p for p in host.glob("/run/lock/*") + host.glob("/var/lock/*")
                   if "dinify" in p and _ok(_ABS_PATH, p))
    return found[:MAX_LOCKS]


def host_facts(host):
    doc = {}
    text, state = _read_text(host, "/etc/os-release", 65536)
    if text is None:
        doc["osState"] = state
    else:
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "PRETTY_NAME":
                _put(doc, "os", value.strip().strip("'\""), _PRETTY_NAME)
    _put(doc, "kernel", host.kernel(), _KERNEL)
    _put(doc, "machine", host.machine(), _MACHINE)
    doc["euid"] = host.euid()
    return doc


def _not_scanned(report):
    apache, includes = report.get("apache") or {}, (report.get("commands") or {}).get("apache_includes") or {}
    scanned = {f.get(k) for f in apache.get("files", []) for k in ("path", "realpath")}
    return sorted(set(p for p in includes.get("files", []) if p not in scanned))[:MAX_ENTRIES]


def main(host=None):
    host = host if host is not None else RealHost()
    report = {"schema": SCHEMA}
    collected = _section(host.now)
    report["collectedAt"] = collected if _ok(_TIMESTAMP, collected) else None
    report["host"] = _section(host_facts, host)
    report["commands"] = _section(commands, host)
    report["interpreters"] = _section(lambda: [interpreter(host, p) for p in INTERPRETERS if host.exists(p)])
    report["modWsgi"] = _section(mod_wsgi, host)
    report["apache"] = _section(apache_config, host)
    if isinstance(report["apache"].get("directives"), list):
        report["apache"]["includesNotScanned"] = _section(_not_scanned, report)
    report["processes"] = _section(processes, host)
    report["projectTrees"] = _section(project_trees, host, report["apache"].get("directives") or [])
    report["accounts"] = _section(accounts, host)
    report["disks"] = _section(disks, host)
    report["paths"] = _section(lambda: [describe(host, p) for p in DESCRIBED_PATHS])
    report["locks"] = _section(locks, host)
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    print("collectorSha256=%s" % collector_sha256(), file=sys.stderr)
    print("reportSha256=%s" % hashlib.sha256((text + "\n").encode()).hexdigest(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
