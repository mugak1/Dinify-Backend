"""READ-ONLY HOST DISCOVERY for the D08 B3 installation profile. NOT RUN AGAINST THE REAL HOST.

For a LATER, owner-approved operator run on the UAT instance (release/CUTOVER.md step 1):

    sudo /usr/bin/python3 -I -B discover_host.py > report.json

It prints ONE JSON document describing what release/profiles/uat-backend.json needs, and on
stderr the sha256 of this collector and of the report — the two values a verified profile's
``observation`` block names. It WRITES NOTHING (no file, no lock, no cache, no bytecode:
``-B``), SENDS NOTHING (no network call of any kind), STARTS NO SERVICE and CHANGES NO
CONFIGURATION. Every external command it runs is a read-only query from a fixed list.

WHAT IT NEVER REPORTS
  * a secret or any configuration VALUE: an environment file is reported by ownership, mode
    and the PRESENCE of a fixed list of key names; the one value read is ENV's word, and only
    when it is dev/test/prod (anything else is reported as "other");
  * a process environment: no process's environment file under /proc is ever opened; command lines are
    read only for Apache and mod_wsgi daemons, whose titles carry no secret;
  * an Apache directive's argument that can carry a secret (SetEnv, PassEnv values,
    Password-like directives): the directive NAME and location are reported, never the value.

It reports what it FOUND, not what should be. Choosing values for the profile — and deciding
the cutover — is the reviewed human step that follows.
"""

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

ENV_KEYS = ("ENV", "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "ADMIN_ALLOWED_HOSTS", "DINER_CAP_KEY", "ADMIN_SECRET_ENCRYPTION_KEY",
            "DATABASE_ENGINE", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST", "DATABASE_PORT",
            "YO_SMS_ACCOUNT_NO", "YO_SMS_PASSWORD", "EMAIL_HOST", "EMAIL_ACCOUNT", "EMAIL_PASSWORD", "OTP_HMAC_PEPPER",
            "MEDIA_ROOT", "STATIC_ROOT", "SECURE_SSL_REDIRECT")
WSGI_DIRECTIVES = ("WSGIDaemonProcess", "WSGIProcessGroup", "WSGIScriptAlias", "WSGIApplicationGroup", "WSGIPassAuthorization",
                   "WSGIPythonHome", "WSGIPythonPath", "WSGIRestrictEmbedded", "WSGISocketPrefix")
SHOWN_DIRECTIVES = WSGI_DIRECTIVES + ("ServerName", "ServerAlias", "Alias", "AliasMatch", "Include", "IncludeOptional",
                                      "DocumentRoot", "Listen", "RewriteRule", "ProxyPass", "Header")
REDACTED_DIRECTIVES = ("SetEnv", "SetEnvIf", "PassEnv", "UnsetEnv")
READ_ONLY_COMMANDS = {
    "apache_version": ["apache2ctl", "-v"],
    "apache_vhosts": ["apache2ctl", "-S"],
    "apache_modules": ["apache2ctl", "-M"],
    "apache_includes": ["apache2ctl", "-t", "-D", "DUMP_INCLUDES"],
    "mod_wsgi_package": ["dpkg-query", "-W", "-f", "${Package} ${Version} ${Status}\\n", "libapache2-mod-wsgi-py3"],
    "python_packages": ["dpkg-query", "-W", "-f", "${Package} ${Version}\\n", "python3.12", "libpython3.12", "python3.12-venv"],
    "aws_cli": ["/usr/local/bin/aws", "--version"],
    "processes": ["ps", "-eo", "user:24,group:24,pid,ppid,etimes,rss,args", "--sort", "pid"],
}


def run(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"})
        return {"exit": proc.returncode, "out": (proc.stdout + proc.stderr)[-12000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exit": None, "out": type(error).__name__}


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as error:
        return "unreadable:%s" % type(error).__name__


def describe(path):
    try:
        st = os.lstat(path)
    except OSError as error:
        return {"path": path, "exists": False, "error": type(error).__name__}
    doc = {"path": path, "exists": True, "type": "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file",
           "owner": _user(st.st_uid), "group": _group(st.st_gid), "mode": "%o" % stat.S_IMODE(st.st_mode)}
    if doc["type"] == "link":
        doc["target"] = os.readlink(path)
    return doc


def _user(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return uid


def _group(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return gid


def env_file(path):
    """Ownership, mode, and which of a fixed list of keys are PRESENT. Values are never kept,
    with the single exception of ENV's word from its fixed vocabulary."""
    doc = describe(path)
    if not doc.get("exists") or doc["type"] != "file":
        return doc
    present, env_word = [], None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                key = key.strip().removeprefix("export ").strip()
                if sep and key in ENV_KEYS:
                    present.append(key)
                    if key == "ENV":
                        word = value.strip().strip("'\"")
                        env_word = word if word in ("dev", "test", "prod") else "other"
                del line, value
    except OSError as error:
        doc["error"] = type(error).__name__
    doc["keysPresent"] = sorted(set(present))
    doc["keysAbsent"] = sorted(set(ENV_KEYS) - set(present))
    doc["envWord"] = env_word
    return doc


def apache_directives():
    """Every enabled Apache configuration file's WSGI/alias/vhost directives, with the value
    of anything that can carry a secret withheld."""
    found = []
    files = sorted(set(glob.glob("/etc/apache2/apache2.conf") + glob.glob("/etc/apache2/sites-enabled/*") +
                       glob.glob("/etc/apache2/conf-enabled/*") + glob.glob("/etc/apache2/mods-enabled/wsgi.*") +
                       glob.glob("/etc/apache2/dinify-backend/*")))
    for path in files:
        real = os.path.realpath(path)
        try:
            with open(real, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().replace("\\\n", " ").splitlines()
        except OSError as error:
            found.append({"file": path, "error": type(error).__name__})
            continue
        for number, line in enumerate(lines, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            name = text.split()[0].lstrip("<").rstrip(">")
            if name in REDACTED_DIRECTIVES or re.search(r"(?i)password|passwd|secret|credential", name):
                found.append({"file": path, "line": number, "directive": name, "value": "<withheld>"})
            elif name in SHOWN_DIRECTIVES or name in ("VirtualHost", "/VirtualHost", "Directory", "Location", "LoadModule"):
                found.append({"file": path, "line": number, "directive": name, "value": text[len(name) + 1:].strip()[:600]})
    return {"files": [describe(p) | {"realpath": os.path.realpath(p), "sha256": sha256(os.path.realpath(p))} for p in files],
            "directives": found}


def interpreter(python):
    code = ("import json,platform,sys,sysconfig;print(json.dumps({'python':platform.python_version(),"
            "'soabi':sysconfig.get_config_var('SOABI'),'ldlibrary':sysconfig.get_config_var('LDLIBRARY'),"
            "'libdir':sysconfig.get_config_var('LIBDIR'),'prefix':sys.prefix,'basePrefix':sys.base_prefix,"
            "'libc':' '.join(platform.libc_ver()),'machine':platform.machine()}))")
    got = run([python, "-I", "-S", "-B", "-c", code])
    doc = {"path": python, "realpath": os.path.realpath(python), "sha256": sha256(os.path.realpath(python))}
    try:
        facts = json.loads(got["out"])
    except ValueError:
        return dict(doc, error="did not run")
    lib = os.path.join(facts.get("libdir") or "", facts.get("ldlibrary") or "")
    return dict(doc, facts=facts, libpython=os.path.realpath(lib), libpythonSha256=sha256(lib))


def mod_wsgi():
    load = {}
    for path in glob.glob("/etc/apache2/mods-enabled/wsgi.load"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "LoadModule" and "wsgi" in parts[1]:
                    load = {"loadFile": os.path.realpath(path), "module": parts[2], "realpath": os.path.realpath(parts[2]),
                            "sha256": sha256(os.path.realpath(parts[2]))}
    needed = run(["readelf", "-d", load["realpath"]]) if load.get("realpath") else None
    if needed:
        load["needed"] = re.findall(r"Shared library: \[([^\]]+)\]", needed["out"])
    return load


def processes():
    rows = run(READ_ONLY_COMMANDS["processes"])["out"].splitlines()
    kept = [rows[0]] + [r for r in rows[1:] if re.search(r"apache2|httpd|\(wsgi:", r)]
    daemons = []
    for row in kept[1:]:
        parts = row.split(None, 6)
        if len(parts) == 7 and parts[6].startswith("(wsgi:"):
            pid = parts[2]
            try:
                cwd = os.readlink("/proc/%s/cwd" % pid)
            except OSError:
                cwd = None
            mapped = set()
            try:
                with open("/proc/%s/maps" % pid) as fh:
                    for line in fh:
                        path = line.split(None, 5)[5].strip() if len(line.split(None, 5)) == 6 else ""
                        if path.endswith((".so", ".py", ".pyc")) or ".so." in path:
                            mapped.add(os.path.dirname(path))
            except OSError:
                pass
            daemons.append({"pid": pid, "title": parts[6][:80], "user": parts[0], "cwd": cwd, "mappedDirs": sorted(mapped)[:60]})
    return {"table": kept[:200], "wsgiDaemons": daemons}


def project_trees(directives):
    """Directories named by WSGI directives (script targets, python-home/-path, home): layout,
    ownership, and the environment files python-decouple's upward search would find."""
    paths = set()
    for d in directives:
        if d.get("directive") in ("WSGIScriptAlias", "WSGIDaemonProcess", "WSGIPythonHome", "WSGIPythonPath", "Alias"):
            for token in re.split(r"[\s:=]+", d.get("value", "")):
                if token.startswith("/") and not token.startswith("/%") and os.path.exists(token):
                    paths.add(token if os.path.isdir(token) else os.path.dirname(token))
    trees = []
    for path in sorted(paths):
        doc = describe(path)
        head = os.path.join(path, ".git", "HEAD")
        if os.path.exists(head):
            with open(head) as fh:
                ref = fh.read().strip()
            doc["gitHead"] = ref
            if ref.startswith("ref: "):
                ref_file = os.path.join(path, ".git", ref[5:])
                if os.path.exists(ref_file):
                    with open(ref_file) as fh:
                        doc["gitCommit"] = fh.read().strip()
        envs, current = [], os.path.realpath(path)
        while True:
            for name in (".env", "settings.ini"):
                candidate = os.path.join(current, name)
                if os.path.lexists(candidate):
                    envs.append(env_file(candidate))
            if os.path.dirname(current) == current:
                break
            current = os.path.dirname(current)
        doc["decoupleWouldFind"] = envs
        trees.append(doc)
    return trees


def accounts():
    wanted = ("root", "ubuntu", "www-data", "postgres", "ssm-user")
    found = {}
    for name in wanted + tuple(p.pw_name for p in pwd.getpwall() if p.pw_name.startswith("dinify")):
        try:
            p = pwd.getpwnam(name)
            found[name] = {"uid": p.pw_uid, "gid": p.pw_gid, "home": p.pw_dir, "shell": p.pw_shell,
                           "groups": sorted(g.gr_name for g in grp.getgrall() if name in g.gr_mem)}
        except KeyError:
            found[name] = None
    return found


def disks():
    found = {}
    for path in ("/", "/srv", "/opt", "/var", "/var/www", "/home", "/tmp"):
        try:
            st = os.statvfs(path)
            found[path] = {"freeBytes": st.f_bavail * st.f_frsize, "totalBytes": st.f_blocks * st.f_frsize, "device": os.stat(path).st_dev}
        except OSError:
            found[path] = None
    return found


def main():
    report = {"schema": "dinify.backend.host-discovery/1", "collectedAt": None, "host": {}}
    report["collectedAt"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open("/etc/os-release") as fh:
            os_release = dict(line.rstrip().split("=", 1) for line in fh if "=" in line)
    except OSError:
        os_release = {}
    report["host"] = {"os": os_release.get("PRETTY_NAME", "").strip('"'), "kernel": platform.release(), "machine": platform.machine(),
                      "euid": os.geteuid()}
    report["commands"] = {k: run(v) for k, v in READ_ONLY_COMMANDS.items() if k != "processes"}
    report["interpreters"] = [interpreter(p) for p in ("/usr/bin/python3.12", "/usr/bin/python3") if os.path.exists(p)]
    report["modWsgi"] = mod_wsgi()
    apache = apache_directives()
    report["apache"] = apache
    report["processes"] = processes()
    report["projectTrees"] = project_trees(apache["directives"])
    report["accounts"] = accounts()
    report["disks"] = disks()
    report["paths"] = [describe(p) for p in ("/etc/apache2/dinify-backend", "/srv", "/opt/dinify-backend-release", "/var/www",
                                             "/var/www/dinify-admin", "/var/www/dinify-admin-releases", "/run/lock", "/var/lock")]
    report["locks"] = sorted(p for p in glob.glob("/run/lock/*") + glob.glob("/var/lock/*") if "dinify" in p)
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    print("collectorSha256=%s" % sha256(os.path.abspath(__file__)), file=sys.stderr)
    print("reportSha256=%s" % hashlib.sha256((text + "\n").encode()).hexdigest(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
