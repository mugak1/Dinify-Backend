"""THE DISCOVERY COLLECTOR'S CONFIDENTIALITY BOUNDARY (D08 B3), on a SYNTHETIC HOST ONLY.

release/staged/discover_host.py will one day run as root on the UAT instance and its report
leaves the host for review. Three paths once carried values out of it: Apache directive
arguments shown verbatim (a ``Header`` holding an API key), a command's stderr folded into its
output, and process-table rows selected by an ``apache2`` SUBSTRING (an unrelated command
line, arguments and all). This suite pins each closed, and pins what the report must still
retain for the profile.

NOTHING HERE READS THIS MACHINE. Every file, link, account, command response and /proc entry
is served by FakeHost; the collector reaches the machine only through RealHost, an AST rule
keeps it that way, and every synthetic test replaces RealHost with a tripwire. The one
exception is RealHostRunner, which drives the real command runner against FIXTURE scripts
this suite writes to a temporary directory, never a system command.
"""

import ast
import builtins
import contextlib
import copy
import errno
import fnmatch
import glob
import grp
import hashlib
import importlib
import io
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from release import hostprofile as hp
from release.staged import discover_host as dh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTOR = os.path.join(ROOT, "release", "staged", "discover_host.py")
PROFILE = os.path.join(ROOT, "release", "profiles", "uat-backend.json")
REAL_HOST = dh.RealHost      # captured before any tripwire replaces it


def source():
    with open(COLLECTOR, encoding="utf-8") as fh:
        return fh.read()

APP = "/home/ubuntu/dinify_backend_handover_uat"
VENV = "/home/ubuntu/venv_uat_handover"
MEDIA = "/var/lib/dinify-media"
LIBPY = "/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0"
MOD = "/usr/lib/apache2/modules/mod_wsgi.so"
COMMIT = "c0ffee" + "0" * 34

# Each canary sits ONLY where a value must never be retained. Lower-case, so a test that
# compares case-insensitively still sees one a collector might have case-folded.
C = {name: "canary-%s-%s" % (name, hashlib.sha256(name.encode()).hexdigest()[:10]) for name in (
    "header", "reqheader", "proxypass", "rewritecond", "rewriterule", "setenv", "setenvif", "passenv", "define",
    "aliasmatch", "lowercase-header", "ldap", "hidden-directive", "daemon-option", "daemon-token", "quoted-alias",
    "unterminated", "servername", "stderr-v", "stderr-s", "stderr-m", "stderr-t", "stderr-dpkg", "stderr-aws",
    "stderr-python", "stderr-readelf", "stdout-v", "stdout-s", "define-dump", "proc-bash", "proc-vim", "proc-spoof",
    "proc-exporter", "apache-arg", "title", "maps", "secret-key", "db-password", "diner-key", "unlisted", "env-word",
    "git", "os-release", "capability", "section-form", "define-vhost", "symlink-env")}


class FakeStat:
    def __init__(self, mode, uid=0, gid=0, dev=2049):
        self.st_mode, self.st_uid, self.st_gid, self.st_dev = mode, uid, gid, dev


class FakeVfs:
    def __init__(self, free):
        self.f_bavail, self.f_frsize, self.f_blocks = free, 4096, free * 4


class FakeHost:
    """The whole machine, as data. Mirrors RealHost's methods exactly (pinned below); an
    unknown command, or any path naming a process environment, is recorded as a violation."""

    def __init__(self):
        self.files, self.dirs, self.links, self.meta, self.specials = {}, {"/"}, {}, {}, {}
        self.executables, self.commands = set(), {}
        self.users_, self.groups_, self.vfs = [], [], {}
        self.euid_, self.kernel_, self.machine_, self.now_ = 0, "6.8.0-1009-aws", "x86_64", "2026-10-15T09:00:00Z"
        self.reads, self.ran, self.violations = [], [], []

    # --- building ---
    def file(self, path, data, uid=0, gid=0, mode=0o644):
        self.files[path] = data.encode() if isinstance(data, str) else data
        self.meta[path] = (uid, gid, mode)
        self._parents(path)

    def dir(self, path, uid=0, gid=0, mode=0o755):
        self.dirs.add(path)
        self.meta[path] = (uid, gid, mode)
        self._parents(path)

    def link(self, path, target):
        self.links[path] = target
        self._parents(path)

    def _parents(self, path):
        parent = path.rsplit("/", 1)[0] or "/"
        while parent not in self.dirs:
            self.dirs.add(parent)
            parent = parent.rsplit("/", 1)[0] or "/"

    # --- RealHost's interface ---
    def _guard(self, path):
        if not isinstance(path, str) or not path.startswith("/") or "environ" in path:
            self.violations.append(("path", path))
            raise AssertionError("the collector asked for %r" % (path,))
        return path.rstrip("/") or "/"

    def _resolve(self, path):
        path = self._guard(path)
        for _ in range(40):
            parts, changed = path.split("/"), False
            for i in range(2, len(parts) + 1):
                prefix = "/".join(parts[:i])
                if prefix in self.links:
                    target = self.links[prefix]
                    base = target if target.startswith("/") else (prefix.rsplit("/", 1)[0] or "") + "/" + target
                    path = os.path.normpath("/".join([base] + parts[i:]))
                    changed = True
                    break
            if not changed:
                return path
        raise OSError(errno.ELOOP, "loop")

    def read(self, path, limit):
        real = self._resolve(path)
        self.reads.append(real)
        if real in self.specials:
            self.violations.append(("opened a non-regular file", real))
            raise AssertionError("a device or pipe was opened")
        if real in self.dirs:
            raise IsADirectoryError(errno.EISDIR, "dir")
        if real not in self.files:
            raise FileNotFoundError(errno.ENOENT, "missing")
        data = self.files[real]
        if isinstance(data, BaseException):
            raise data
        return data[:limit + 1]

    def lstat(self, path):
        path = self._guard(path)
        uid, gid, mode = self.meta.get(path, (0, 0, 0o644))
        if path in self.links:
            return FakeStat(stat.S_IFLNK | 0o777)
        if path in self.dirs:
            return FakeStat(stat.S_IFDIR | (mode if path in self.meta else 0o755), uid, gid)
        if path in self.files:
            return FakeStat(stat.S_IFREG | mode, uid, gid)
        if path in self.specials:
            return FakeStat(self.specials[path] | 0o666)
        raise FileNotFoundError(errno.ENOENT, "missing")

    def stat(self, path):
        return self.lstat(self._resolve(path))

    def readlink(self, path):
        path = self._guard(path)
        if path not in self.links:
            raise OSError(errno.EINVAL if path in self.files or path in self.dirs else errno.ENOENT, "not a link")
        target = self.links[path]
        if isinstance(target, BaseException):
            raise target
        return target

    def realpath(self, path):
        try:
            return self._resolve(path)
        except OSError:
            return path

    def exists(self, path):
        real = self.realpath(path)
        return real in self.files or real in self.dirs or real in self.specials

    def lexists(self, path):
        path = self._guard(path)
        return path in self.files or path in self.dirs or path in self.links

    def isdir(self, path):
        return self.realpath(path) in self.dirs

    def glob(self, pattern):
        parts = pattern.split("/")
        names = set(self.files) | self.dirs | set(self.links)
        return sorted(n for n in names if len(n.split("/")) == len(parts) and
                      all(fnmatch.fnmatchcase(a, b) and not (a.startswith(".") and not b.startswith("."))
                          for a, b in zip(n.split("/"), parts)))

    def listdir(self, path):
        path = self._guard(path)
        if path not in self.dirs:
            raise FileNotFoundError(errno.ENOENT, "missing")
        prefix = path.rstrip("/") + "/"
        names = set(self.files) | self.dirs | set(self.links)
        return sorted({n[len(prefix):].split("/")[0] for n in names if n.startswith(prefix) and n != path})

    def sha256(self, path):
        return hashlib.sha256(self.read(path, 1 << 30)).hexdigest()

    def which(self, name):
        for candidate in ([name] if name.startswith("/") else [d + "/" + name for d in dh.COMMAND_PATH]):
            if candidate in self.executables:
                return candidate
        return None

    def run(self, argv, timeout=dh.COMMAND_TIMEOUT):
        self.ran.append(tuple(argv))
        response = self.commands.get(tuple(argv))
        if response is None:
            self.violations.append(("command", tuple(argv)))
            return {"state": "unavailable", "exit": None, "stdout": "", "stderr": ""}
        if response in ("timeout", "unavailable"):
            return {"state": response, "exit": None, "stdout": "", "stderr": ""}
        if isinstance(response, BaseException):
            raise response
        rc, out, err = response
        return {"state": "ok" if rc == 0 else "nonzero", "exit": rc, "stdout": out, "stderr": err}

    def statvfs(self, path):
        if path not in self.vfs:
            raise FileNotFoundError(errno.ENOENT, "missing")
        return self.vfs[path]

    def users(self):
        return list(self.users_)

    def groups(self):
        return list(self.groups_)

    def user_name(self, uid):
        return next((u[0] for u in self.users_ if u[1] == uid), None)

    def group_name(self, gid):
        return next((g[0] for g in self.groups_ if g[1] == gid), None)

    def euid(self):
        return self.euid_

    def kernel(self):
        return self.kernel_

    def machine(self):
        return self.machine_

    def now(self):
        return self.now_


def _cmd(*argv):
    return tuple(argv)


CTL = "/usr/sbin/apache2ctl"
DPKG = "/usr/bin/dpkg-query"
INTERP_ARGS = ("-I", "-S", "-B", "-c", dh.INTERPRETER_CODE)

CUSTOMER_SITE = """<IfModule mod_ssl.c>
<VirtualHost *:443>
    ServerName api-test.dinifyapp.com
    ServerAlias www.api-test.dinifyapp.com
    DocumentRoot /var/www/html
    Header always set X-Api-Key "{header}"
    RequestHeader set Authorization "Bearer {reqheader}"
    ProxyPass /hooks/ http://svc:{proxypass}@127.0.0.1:9000/
    ProxyPassReverse /hooks/ http://127.0.0.1:9000/
    RewriteEngine On
    RewriteCond %{{HTTP:X-Token}} !^{rewritecond}$
    RewriteRule ^/legacy/(.*)$ /uat/$1?key={rewriterule} [R=302,L]
    SetEnv DJANGO_TOKEN {setenv}
    SetEnvIf Host "{setenvif}" internal
    PassEnv {passenv}
    AliasMatch "^/x/({aliasmatch})$" /srv/x/$1
    header set X-Lowercase {lowercase-header}
    AuthLDAPBindPassword {ldap}
    Alias /uat/static/ {app}/static/
    Alias /uat/media/ {media}/
    Alias /broken/ "/srv/odd dir {quoted-alias}/"
    Alias /unterminated/ "/srv/{unterminated}
    <Location /hooks/{capability}>
        ProxyPass http://127.0.0.1:9000/
    </Location>
    Alias /drop/{capability}/ /srv/drop/
    <Directory {app}/static>
        Require all granted
    </Directory>
    WSGIDaemonProcess dinify-uat user=www-data group=www-data processes=2 threads=8 display-name=%{{GROUP}} \\
        python-home={venv} python-path={app} home={app} shutdown-timeout=20 lang=C.UTF-8 locale=C.UTF-8
    WSGIProcessGroup dinify-uat
    WSGIApplicationGroup %{{GLOBAL}}
    WSGIScriptAlias /uat {app}/dinify_backend/wsgi.py process-group=dinify-uat application-group=%{{GLOBAL}}
    WSGIPassAuthorization On
    <Location /uat>
        Require all granted
    </Location>
    SSLCertificateFile /etc/letsencrypt/live/api-test.dinifyapp.com/fullchain.pem
</VirtualHost>
</IfModule>
"""
ADMIN_SITE = """<VirtualHost *:443>
    ServerName admin.dinifyapp.com
    DocumentRoot /var/www/dinify-admin
    WSGIDaemonProcess dinify-admin user=www-data group=www-data processes=1 threads=4 \\
        python-home={venv} python-path={app} home={app} shutdown-timeout=15 x-token={daemon-option} {daemon-token}
    WSGIScriptAlias /api {app}/dinify_backend/wsgi_admin.py process-group=dinify-admin application-group=%{{GLOBAL}}
    WSGIPassAuthorization On
    Alias /api/static/ {app}/static/
    ServerName bad$name{servername}
    <ServerName {section-form}>
</VirtualHost>
"""
APACHE_CONF = """# global
ServerRoot "/etc/apache2"
Include ports.conf
IncludeOptional mods-enabled/*.load
IncludeOptional sites-enabled/*.conf
Define SECRET_DEFINE {define}
<IfModule mod_nonexistent.c>
    MyPasswordToken {hidden-directive}
</IfModule>
"""
VHOSTS_DUMP = """VirtualHost configuration:
*:443                  is a NameVirtualHost
         default server api-test.dinifyapp.com (/etc/apache2/sites-enabled/api-test-le-ssl.conf:2)
         port 443 namevhost api-test.dinifyapp.com (/etc/apache2/sites-enabled/api-test-le-ssl.conf:2)
                 alias www.api-test.dinifyapp.com
         port 443 namevhost admin.dinifyapp.com (/etc/apache2/sites-enabled/admin-le-ssl.conf:1)
*:80                   api-test.dinifyapp.com (/etc/apache2/sites-enabled/000-default.conf:1)
ServerRoot: "/etc/apache2"
Main DocumentRoot: "/var/www/html"
Main ErrorLog: "/var/log/apache2/error.log"
Mutex default: dir="/var/run/apache2/" mechanism=default
PidFile: "/var/run/apache2/apache2.pid"
Define: DUMP_VHOSTS
Define: SECRET_DEFINE={define-dump}
Define: {define-vhost} (/etc/apache2/a.conf:1)
note {stdout-s}
User: name="www-data" id=33
Group: name="www-data" id=33
"""
INCLUDES_DUMP = """Included configuration files:
  (*) /etc/apache2/apache2.conf
    (146) /etc/apache2/mods-enabled/wsgi.load
    (146) /etc/apache2/mods-enabled/wsgi.conf
    (147) /etc/apache2/ports.conf
    (225) /etc/apache2/sites-enabled/000-default.conf
    (225) /etc/apache2/sites-enabled/admin-le-ssl.conf
    (225) /etc/apache2/sites-enabled/api-test-le-ssl.conf
    (226) /etc/letsencrypt/options-ssl-apache.conf
"""
INTERP_FACTS = {"python": "3.12.3", "implementation": "CPython", "soabi": "cpython-312-x86_64-linux-gnu",
                "ldlibrary": "libpython3.12.so", "instsoname": "libpython3.12.so.1.0", "libdir": "/usr/lib/x86_64-linux-gnu",
                "prefix": "/usr", "basePrefix": "/usr", "isVenv": False, "libc": "glibc 2.39", "machine": "x86_64"}
READELF_OUT = """
Dynamic section at offset 0x1d000 contains 27 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libpython3.12.so.1.0]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000000e (SONAME)             Library soname: [mod_wsgi.so]
"""
ENV_FILE = """SECRET_KEY={secret-key}
ENV=dev
DATABASE_PASSWORD='{db-password}'
export DINER_CAP_KEY="{diner-key}"
MEDIA_ROOT={media}
# ENV=prod
UNLISTED_KEY={unlisted}
"""
MAPS = """7f0000000000-7f0000001000 r-xp 00000000 08:01 1 {libpy}
7f0000001000-7f0000002000 r-xp 00000000 08:01 2 {venv}/lib/python3.12/site-packages/psycopg_binary/_psycopg.cpython-312-x86_64-linux-gnu.so
7f0000002000-7f0000003000 rw-p 00000000 00:00 0 [heap]
7f0000003000-7f0000004000 r-xp 00000000 08:01 3 /tmp/odd dir {maps}/lib.so
"""


def fill(template):
    return template.format(app=APP, venv=VENV, media=MEDIA, libpy=LIBPY, **C)


def uat_like_host():
    """A synthetic stand-in for the UAT instance: both planes, both their facts, and a canary
    wherever a value must not be retained."""
    h = FakeHost()
    h.users_ = [("root", 0, 0, "/root", "/bin/bash"), ("ubuntu", 1000, 1000, "/home/ubuntu", "/bin/bash"),
                ("www-data", 33, 33, "/var/www", "/usr/sbin/nologin")]
    h.groups_ = [("root", 0, []), ("www-data", 33, ["ubuntu"]), ("ubuntu", 1000, []), ("adm", 4, ["ubuntu"])]
    h.file("/etc/os-release", 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\n')
    h.file("/etc/apache2/apache2.conf", fill(APACHE_CONF))
    h.file("/etc/apache2/ports.conf", "Listen 80\n<IfModule ssl_module>\n    Listen 443 https\n</IfModule>\n")
    h.file("/etc/apache2/sites-available/api-test-le-ssl.conf", fill(CUSTOMER_SITE))
    h.link("/etc/apache2/sites-enabled/api-test-le-ssl.conf", "../sites-available/api-test-le-ssl.conf")
    h.file("/etc/apache2/sites-enabled/admin-le-ssl.conf", fill(ADMIN_SITE))
    h.file("/etc/apache2/sites-enabled/000-default.conf",
           "<VirtualHost *:80>\n    ServerName api-test.dinifyapp.com\n"
           "    RewriteRule ^ https://%{SERVER_NAME}%{REQUEST_URI} [END,NE,R=permanent]\n</VirtualHost>\n")
    h.file("/etc/apache2/sites-enabled/README", "ServerName stale.example.test\n")   # matched by the glob, not included
    h.dir("/etc/apache2/conf-enabled")
    h.file("/etc/apache2/mods-enabled/wsgi.load", "LoadModule wsgi_module %s\n" % MOD)
    h.file("/etc/apache2/mods-enabled/wsgi.conf", "<IfModule mod_wsgi.c>\n    WSGIRestrictEmbedded On\n</IfModule>\n")
    h.file(MOD, b"\x7fELF synthetic mod_wsgi")
    h.file(LIBPY, b"\x7fELF synthetic libpython")
    h.file("/usr/bin/python3.12", b"\x7fELF synthetic python", mode=0o755)
    h.link("/usr/bin/python3", "python3.12")
    for exe in (CTL, DPKG, "/usr/local/bin/aws", "/usr/bin/readelf", "/usr/bin/python3.12", "/usr/bin/python3"):
        h.executables.add(exe)
    h.file(APP + "/dinify_backend/wsgi.py", "application = None\n", uid=1000, gid=1000)
    h.file(APP + "/dinify_backend/wsgi_admin.py", "application = None\n", uid=1000, gid=1000)
    h.dir(APP, uid=1000, gid=1000)
    h.dir(APP + "/static", uid=1000, gid=1000)
    h.file(APP + "/.git/HEAD", COMMIT + "\n", uid=1000, gid=1000)
    h.file(APP + "/.env", fill(ENV_FILE), uid=1000, gid=33, mode=0o640)
    h.dir(VENV, uid=1000, gid=1000)
    h.dir(MEDIA, uid=33, gid=33)
    for path in ("/srv", "/opt", "/var/www", "/var/www/dinify-admin", "/run/lock", "/var/lock", "/tmp", "/home"):
        h.dir(path)
    h.file("/run/lock/dinify-host.lock", "")
    h.vfs = {p: FakeVfs(1 << 20) for p in dh.DISK_PATHS}
    # commands, each with a canary on stderr
    h.commands = {
        _cmd(CTL, "-v"): (0, "Server version: Apache/2.4.58 (Ubuntu)\nServer built:   2024-10-02T12:40:51\n%s\n" % C["stdout-v"],
                          "AH00558: apache2: could not determine %s\n" % C["stderr-v"]),
        _cmd(CTL, "-S"): (0, fill(VHOSTS_DUMP), "AH00558: %s\n" % C["stderr-s"]),
        _cmd(CTL, "-M"): (0, "Loaded Modules:\n core_module (static)\n so_module (static)\n wsgi_module (shared)\n ssl_module (shared)\n",
                          "warn %s\n" % C["stderr-m"]),
        _cmd(CTL, "-t", "-D", "DUMP_INCLUDES"): (0, INCLUDES_DUMP, "Syntax OK\nwarn %s\n" % C["stderr-t"]),
        _cmd(DPKG, *dh.READ_ONLY_COMMANDS["mod_wsgi_package"][1:]):
            (0, "libapache2-mod-wsgi-py3 5.0.0-1build2 install ok installed\n", ""),
        _cmd(DPKG, *dh.READ_ONLY_COMMANDS["python_packages"][1:]):
            (1, "python3.12 3.12.3-1ubuntu0.8\npython3.12-venv \n",
             "dpkg-query: no packages found matching libpython3.12 %s\n" % C["stderr-dpkg"]),
        _cmd("/usr/local/bin/aws", "--version"): (0, "aws-cli/2.15.0 Python/3.11.6 Linux/6.8.0-1009-aws exe/x86_64.ubuntu.24\n",
                                                  "%s\n" % C["stderr-aws"]),
        _cmd("/usr/bin/python3.12", *INTERP_ARGS): (0, json.dumps(INTERP_FACTS) + "\n", "%s\n" % C["stderr-python"]),
        _cmd("/usr/bin/python3", *INTERP_ARGS): (0, json.dumps(INTERP_FACTS) + "\n", ""),
        _cmd("/usr/bin/readelf", "-d", MOD): (0, READELF_OUT, "readelf: %s\n" % C["stderr-readelf"]),
    }
    # processes: Apache's own, and every way something else can look like it
    procs = {
        1: ("/usr/lib/systemd/systemd", b"/sbin/init\0", 0, 0, 0),
        100: ("/usr/sbin/apache2", ("/usr/sbin/apache2\0-k\0start\0-D\0%s\0" % C["apache-arg"]).encode(), 0, 0, 1),
        101: ("/usr/sbin/apache2", b"(wsgi:dinify-uat)" + b" " * 20 + b"\0", 33, 33, 100),
        102: ("/usr/sbin/apache2 (deleted)", b"/usr/sbin/apache2\0-k\0start\0", 33, 33, 100),   # no display-name
        103: ("/usr/sbin/apache2", b"/usr/sbin/apache2\0-k\0start\0", 33, 33, 100),
        104: ("/usr/sbin/apache2", ("(wsgi:bad group %s)" % C["title"]).encode(), 33, 33, 100),
        200: ("/usr/bin/bash", ("bash\0-c\0curl -H 'Authorization: Bearer %s' https://x.test/apache2\0" % C["proc-bash"]).encode(),
              1000, 1000, 1),
        201: ("/usr/bin/vim.basic", ("vim\0/etc/apache2/sites-enabled/%s.conf\0" % C["proc-vim"]).encode(), 1000, 1000, 1),
        202: ("/usr/bin/python3.12", ("(wsgi:spoofed) %s\0" % C["proc-spoof"]).encode(), 1000, 1000, 1),
        203: ("/usr/local/bin/apache2-exporter", ("apache2-exporter\0--token=%s\0" % C["proc-exporter"]).encode(), 65534, 65534, 1),
    }
    for pid, (exe, cmdline, uid, gid, ppid) in procs.items():
        h.link("/proc/%d/exe" % pid, exe)
        h.file("/proc/%d/cmdline" % pid, cmdline)
        h.file("/proc/%d/status" % pid, "Name:\tx\nPPid:\t%d\nUid:\t%d\t%d\t%d\t%d\nGid:\t%d\t%d\t%d\t%d\n"
               % (ppid, uid, uid, uid, uid, gid, gid, gid, gid))
    h.dir("/proc/2")                                   # a kernel thread: no executable link
    h.link("/proc/101/cwd", APP)
    h.file("/proc/101/maps", fill(MAPS))
    h.link("/proc/102/cwd", APP)
    h.file("/proc/102/maps", "1-2 r-xp 0 08:01 1 %s-3.12\n2-3 r--p 0 08:01 2 %s/lib/python3.12/site-packages/django/__init__.py\n"
           "3-4 r-xp 0 08:01 3 %s (deleted)\n" % (MOD, VENV, LIBPY))
    h.link("/proc/103/cwd", "/")
    h.file("/proc/103/maps", "1-2 r-xp 0 08:01 1 %s-3.12\n3-4 r-xp 0 08:01 3 %s\n" % (MOD, LIBPY))
    h.link("/proc/104/cwd", "/")
    h.file("/proc/104/maps", "")
    return h


def run_main(host):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dh.main(host)
    return code, out.getvalue(), err.getvalue()


def collect(host):
    code, stdout, stderr = run_main(host)
    return json.loads(stdout), stdout, stderr


class _ForbiddenHost:
    def __init__(self, *a, **k):
        raise AssertionError("a synthetic test reached the real host")


class Synthetic(unittest.TestCase):
    """Every synthetic test runs with RealHost replaced by a tripwire."""

    def setUp(self):
        patcher = mock.patch.object(dh, "RealHost", _ForbiddenHost)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertNoCanary(self, *texts, names=None):
        blob = "\n".join(texts).lower()
        for name in (names or C):
            self.assertNotIn(C[name], blob, "canary %r reached the output" % name)

    def directives(self, report, name):
        return [d for d in report["apache"]["directives"] if d.get("directive") == name]


ALLOWED_IMPORTS = ["builtins", "configparser", "errno", "glob", "grp", "hashlib", "json", "os", "platform", "pwd", "re", "stat",
                   "subprocess", "sys"]
FORBIDDEN_NAMES = {"os", "subprocess", "glob", "pwd", "grp", "platform", "open", "__import__", "__builtins__", "eval", "exec",
                   "compile", "importlib", "pathlib", "io", "shutil", "socket", "globals", "vars", "breakpoint", "input"}


def boundary_offenders(text):
    """What lets code outside RealHost (and the collector hashing ITSELF) reach the machine:
    an import other than the exact top-level set, any alias or from-import, a nested import,
    a forbidden name, ``builtins`` outside ``_error_type``, or ``sys`` beyond stderr and exit."""
    offenders, imports = [], []
    for node in ast.parse(text).body:
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
            offenders += ["aliased import %s" % a.name for a in node.names if a.asname]
            continue
        if isinstance(node, ast.ImportFrom):
            offenders.append("from-import line %d" % node.lineno)
            continue
        if isinstance(node, ast.ClassDef) and node.name == "RealHost" or \
                isinstance(node, ast.FunctionDef) and node.name == "collector_sha256":
            continue
        builtins_ok = isinstance(node, ast.FunctionDef) and node.name == "_error_type"
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                offenders.append("nested import line %d" % sub.lineno)
            elif isinstance(sub, ast.Name) and (sub.id in FORBIDDEN_NAMES or sub.id == "builtins" and not builtins_ok):
                offenders.append("%s line %d" % (sub.id, sub.lineno))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "sys" \
                    and sub.attr not in ("stderr", "exit"):
                offenders.append("sys.%s line %d" % (sub.attr, sub.lineno))
    if sorted(imports) != ALLOWED_IMPORTS:
        offenders.append("imports %s" % sorted(set(imports) ^ set(ALLOWED_IMPORTS)))
    return offenders


class TheBoundary(Synthetic):
    def test_CONTRACT_only_RealHost_reaches_the_machine(self):
        self.assertEqual(boundary_offenders(source()), [])

    def test_CONTRACT_the_boundary_rule_sees_each_way_around_it(self):
        base = source()
        for evasion in ("def f():\n    builtins.open('/etc/x')\n", "import subprocess as sp\n", "from os import listdir\n",
                        "def f():\n    sys.modules['os']\n", "def f():\n    __import__('os')\n", "import pathlib\n",
                        "def f():\n    import shutil\n", "def f():\n    open('/etc/x')\n", "def f():\n    io.open('/etc/x')\n"):
            self.assertTrue(boundary_offenders(base + "\n" + evasion), evasion)

    def test_CONTRACT_a_synthetic_run_makes_no_real_system_call(self):
        """With FakeHost injected, every real primitive the collector could reach is a trap: a
        call is recorded (a raise alone would be swallowed into collector_error)."""
        calls, real_open = [], builtins.open

        def guarded_open(file, *args, **kwargs):
            if os.path.abspath(str(file)) == COLLECTOR:          # the collector hashing itself
                return real_open(file, *args, **kwargs)
            calls.append(("open", str(file)))
            raise AssertionError("open")

        def trap(name):
            def call(*args, **kwargs):
                calls.append((name, args[:1]))
                raise AssertionError(name)
            return call

        targets = ((os, "listdir"), (os, "scandir"), (os, "stat"), (os, "lstat"), (os, "readlink"), (os, "statvfs"),
                   (os, "access"), (subprocess, "Popen"), (glob, "glob"), (pwd, "getpwall"), (pwd, "getpwuid"),
                   (grp, "getgrall"), (grp, "getgrgid"), (platform, "release"), (platform, "machine"))
        host = uat_like_host()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("builtins.open", guarded_open))
            for module, attr in targets:
                stack.enter_context(mock.patch.object(module, attr, trap("%s.%s" % (module.__name__, attr))))
            code, stdout, stderr = run_main(host)
        self.assertEqual(calls, [])
        self.assertEqual(code, 0)
        self.assertNotIn("collector_error", stdout)

    def test_CONTRACT_the_tripwire_is_armed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(AssertionError):
            dh.main()                                  # no host given: it would build RealHost
        self.assertEqual(out.getvalue(), "")

    def test_CONTRACT_the_fake_host_offers_exactly_what_the_real_one_does(self):
        public = lambda cls: {n for n in vars(cls) if not n.startswith("_") and callable(getattr(cls, n))}  # noqa: E731
        builders = {"file", "dir", "link"}
        self.assertEqual(public(REAL_HOST), public(FakeHost) - builders)

    def test_CONTRACT_the_collector_never_names_a_process_environment(self):
        self.assertNotIn("/environ", source())
        host = uat_like_host()
        collect(host)
        self.assertEqual(host.violations, [])
        self.assertFalse([p for p in host.reads if p.endswith("environ")])


class TheDemonstratedLeaks(Synthetic):
    """The three paths reproduced against be9fff9 with synthetic inputs, closed."""

    def test_REGRESSION_a_header_value_never_leaves_its_directive(self):
        report, stdout, stderr = collect(uat_like_host())
        self.assertNoCanary(stdout, stderr, names=["header"])
        self.assertNotIn("X-Api-Key", stdout)
        headers = self.directives(report, "Header")
        self.assertTrue(headers, "the Header line was seen")
        for record in headers:
            self.assertLessEqual(set(record), {"file", "line", "directive", "vhost", "state", "conditional"})
            self.assertEqual(record["state"], "withheld")

    def test_REGRESSION_command_stderr_never_reaches_the_report(self):
        report, stdout, stderr = collect(uat_like_host())
        self.assertNoCanary(stdout, stderr, names=[n for n in C if n.startswith("stderr-")])
        for key in ("apache_version", "apache_vhosts", "apache_modules", "apache_includes", "python_packages", "aws_cli"):
            self.assertEqual(report["commands"][key]["stderr"], "present", key)
        self.assertEqual(report["interpreters"][0]["stderr"], "present")

    def test_REGRESSION_an_unrelated_process_mentioning_apache2_is_not_reported(self):
        host = uat_like_host()
        report, stdout, stderr = collect(host)
        self.assertNoCanary(stdout, stderr, names=["proc-bash", "proc-vim", "proc-spoof", "proc-exporter", "apache-arg"])
        self.assertEqual(sorted(p["pid"] for p in report["processes"]["apache"]), [100, 101, 102, 103, 104])
        # their command lines were never even read
        for pid in (1, 200, 201, 202, 203):
            self.assertNotIn("/proc/%d/cmdline" % pid, host.reads)
            self.assertNotIn("/proc/%d/status" % pid, host.reads)
        for entry in report["processes"]["apache"]:
            self.assertFalse({"args", "title", "command", "cmdline"} & set(entry))


class ArgumentBearingDirectives(Synthetic):
    def test_REGRESSION_every_argument_bearing_or_malformed_directive_keeps_its_value_private(self):
        report, stdout, stderr = collect(uat_like_host())
        self.assertNoCanary(stdout, stderr)
        withheld = {d["directive"] for d in report["apache"]["directives"] if d.get("state") == "withheld"}
        self.assertTrue({"Header", "RequestHeader", "ProxyPass", "ProxyPassReverse", "RewriteCond", "RewriteRule", "SetEnv",
                         "SetEnvIf", "PassEnv", "Define", "AliasMatch"} <= withheld)

    def test_REGRESSION_an_unreviewed_directive_is_counted_never_named(self):
        report, stdout, _ = collect(uat_like_host())
        for name in ("AuthLDAPBindPassword", "MyPasswordToken", "SSLCertificateFile", "RewriteEngine", "ServerRoot"):
            self.assertNotIn(name, stdout)
        named = {d["directive"] for d in report["apache"]["directives"]}
        self.assertLessEqual(named, set(dh.PARSERS) | set(dh.REDACTED_DIRECTIVES))
        self.assertFalse(named & {"Location", "Directory", "DocumentRoot", "Include", "IncludeOptional", "LoadModule"},
                         "a directive the profile does not need is counted, not parsed")
        files = {f["path"]: f for f in report["apache"]["files"]}
        self.assertGreater(files["/etc/apache2/apache2.conf"]["otherDirectives"], 0)

    def test_REGRESSION_a_case_variant_is_the_same_directive(self):
        report, _, _ = collect(uat_like_host())
        lowercase = [d for d in self.directives(report, "Header") if d["line"] == 17]
        self.assertEqual([d["state"] for d in lowercase], ["withheld"])

    def test_REGRESSION_a_reviewed_directive_outside_its_grammar_is_unsupported_not_echoed(self):
        report, _, _ = collect(uat_like_host())
        aliases = {d.get("url"): d for d in self.directives(report, "Alias")}
        self.assertEqual(aliases["/broken/"]["unsupported"], ["path"])
        self.assertNotIn("path", aliases["/broken/"])
        unterminated = [d for d in self.directives(report, "Alias") if d["state"] == "unsupported" and not {"url", "path"} & set(d)]
        self.assertEqual([d["unsupported"] for d in unterminated], [["arguments"]])
        names = [d for d in self.directives(report, "ServerName") if d.get("vhost") == 1]      # admin-le-ssl.conf
        self.assertTrue(any(d.get("unsupported") == ["name"] for d in names))
        admin = next(d for d in self.directives(report, "WSGIDaemonProcess") if d.get("name") == "dinify-admin")
        self.assertEqual(admin["unreviewedOptions"], 2)

    def test_REGRESSION_a_malformed_git_head_or_os_release_is_unsupported_not_echoed(self):
        host = uat_like_host()
        host.file(APP + "/.git/HEAD", "ref: refs/heads/../%s\n" % C["git"])
        host.file("/etc/os-release", 'PRETTY_NAME="Ubuntu $(%s)"\n' % C["os-release"])
        report, stdout, stderr = collect(host)
        self.assertNoCanary(stdout, stderr)
        self.assertEqual(next(t for t in report["projectTrees"] if t["path"] == APP)["gitState"], "unsupported")
        self.assertEqual(report["host"]["unsupported"], ["os"])

    def test_REGRESSION_a_debian_version_yields_its_upstream_part_only_when_exact(self):
        for version, upstream in (("5.0.0-1build2", "5.0.0"), ("1:5.0.0-1ubuntu0.1~24.04", "5.0.0"), ("5.0.0", "5.0.0"),
                                  ("4.9.0+dfsg-1", None), ("5.0-2", None), ("5.0.0-rc1-1", None)):
            self.assertEqual(dh.debian_upstream(version), upstream, version)
        parsed = dh.parse_dpkg("libapache2-mod-wsgi-py3 4.9.0+dfsg-1 install ok installed\n", "")
        self.assertEqual(parsed["packages"][0]["unsupported"], ["upstreamVersion"])
        self.assertNotIn("upstreamVersion", parsed["packages"][0])

    def test_REGRESSION_a_capability_url_is_not_a_mount(self):
        report, stdout, stderr = collect(uat_like_host())
        self.assertNoCanary(stdout, stderr, names=["capability"])
        drop = next(d for d in self.directives(report, "Alias") if d.get("path") == "/srv/drop/")
        self.assertEqual((drop["state"], drop["unsupported"]), ("unsupported", ["url"]))
        self.assertFalse(dh._URL_PATH.fullmatch("/hooks/9f8e7d6c5b4a39281706f5e4d3c2b1a0"))
        for mount in ("/uat", "/api", "/uat/static/", "/uat/media/", "/"):
            self.assertTrue(dh._URL_PATH.fullmatch(mount), mount)

    def test_REGRESSION_only_a_virtualhost_is_read_in_section_form(self):
        parsed = dh.parse_config_text("<ServerName leaked-value-123>\n<Alias /a /b>\n", "/etc/apache2/x.conf")
        self.assertEqual((parsed["directives"], parsed["otherDirectives"]), ([], 2))
        self.assertNoCanary(collect(uat_like_host())[1], names=["section-form"])

    def test_REGRESSION_a_directive_inside_a_conditional_section_says_so(self):
        parsed = dh.parse_config_text("<IfModule !mod_x.c>\n  <VirtualHost *:80>\n    ServerName a.test\n  </VirtualHost>\n"
                                      "</IfModule>\nServerName b.test\n", "/etc/apache2/x.conf")
        self.assertEqual([(d["name"], d.get("conditional")) for d in parsed["directives"]], [("a.test", True), ("b.test", None)])
        self.assertTrue(parsed["vhosts"][0]["conditional"])

    def test_CONTROL_a_repeated_option_is_ambiguous_never_resolved(self):
        parsed = dh.parse_config_text("WSGIDaemonProcess g processes=2 processes=8 threads=4\n", "/etc/apache2/x.conf")
        record = parsed["directives"][0]
        self.assertNotIn("processes", record["options"])
        self.assertEqual(record["unsupported"], ["options.processes"])
        self.assertEqual(record["options"]["threads"], 4)


class CommandOutcomes(Synthetic):
    def outcome(self, response, key="apache_version", parser=None):
        host = FakeHost()
        host.executables.add(CTL)
        host.commands[_cmd(CTL, "-v")] = response
        return dh.command_fact(host, dh.READ_ONLY_COMMANDS[key], parser or dh.parse_apache_version)

    def test_REGRESSION_each_failure_is_a_fixed_state_and_nothing_else(self):
        cases = (("timeout", {"state": "timeout", "exit": None}),
                 ("unavailable", {"state": "unavailable", "exit": None}),
                 ((7, "garbage %s\n" % C["stdout-v"], "boom %s\n" % C["stderr-v"]),
                  {"state": "nonzero", "exit": 7, "stderr": "present", "unparsedLines": 1}))
        for response, expected in cases:
            fact = self.outcome(response)
            for key, value in expected.items():
                self.assertEqual(fact[key], value, (response, key))
            self.assertNoCanary(json.dumps(fact))

    def test_REGRESSION_a_define_shaped_like_a_vhost_is_not_a_vhost(self):
        doc = dh.parse_apache_vhosts(fill(VHOSTS_DUMP), "")
        self.assertNotIn(C["define-vhost"], json.dumps(doc))
        self.assertEqual(len(doc["vhosts"]), 4)
        before = dh.parse_apache_vhosts("Define: secret-token-9f8e (/etc/a.conf:1)\n", "")
        self.assertEqual((before["vhosts"], before["unparsedLines"]), ([], 1))       # no header: no vhost table

    def test_REGRESSION_a_command_that_is_not_installed_is_unavailable_and_not_run(self):
        host = FakeHost()
        self.assertEqual(dh.command_fact(host, dh.READ_ONLY_COMMANDS["apache_version"], dh.parse_apache_version),
                         {"state": "unavailable"})
        self.assertEqual(host.ran, [])

    def test_REGRESSION_an_exception_inside_a_section_prints_no_message(self):
        host = uat_like_host()
        message = "boom %s" % C["unlisted"]
        host.listdir = mock.Mock(side_effect=RuntimeError(message))
        host.commands[_cmd(CTL, "-M")] = ValueError(message)
        report, stdout, stderr = collect(host)
        self.assertEqual(report["processes"], {"state": "collector_error", "errorType": "RuntimeError"})
        self.assertEqual(report["commands"]["apache_modules"], {"state": "collector_error", "errorType": "ValueError"})
        self.assertNoCanary(stdout, stderr)
        self.assertEqual(len(stderr.splitlines()), 2)

    def test_REGRESSION_interpreter_output_is_parsed_never_echoed(self):
        bad = dict(INTERP_FACTS, soabi="cpython 312 %s" % C["unlisted"], extra=C["unlisted"], isVenv="no")
        for stdout, want in ((json.dumps(bad), {"unsupported": ["soabi", "isVenv"]}),
                             ("not json %s" % C["unlisted"], None),
                             (json.dumps([C["unlisted"]]), None)):
            host = uat_like_host()
            host.commands[_cmd("/usr/bin/python3.12", *INTERP_ARGS)] = (0, stdout, "")
            doc = dh.interpreter(host, "/usr/bin/python3.12")
            self.assertNoCanary(json.dumps(doc))
            if want is None:
                self.assertEqual((doc["facts"], doc["factsState"]), (None, "unparseable"))
                self.assertNotIn("libpython", doc)
            else:
                self.assertEqual(doc["facts"]["unsupported"], want["unsupported"])
                self.assertNotIn("soabi", doc["facts"])

    def test_REGRESSION_a_missing_readelf_leaves_needed_unknown_not_empty(self):
        host = uat_like_host()
        host.executables.discard("/usr/bin/readelf")
        doc = dh.mod_wsgi(host)
        self.assertEqual(doc["neededState"], "unavailable")
        self.assertNotIn("needed", doc)

    def test_REGRESSION_mod_wsgi_load_states(self):
        for text, state in (("", "noLoadModule"),
                            ("LoadModule wsgi_module /a/mod_wsgi.so\nLoadModule wsgi_module /b/mod_wsgi.so\n", "ambiguous"),
                            ("LoadModule wsgi_module modules/mod_wsgi.so\n", "unsupported"),
                            ("LoadModule wsgi_module /x/%s.so\n" % "a b", "noLoadModule")):
            host = uat_like_host()
            host.file(dh.WSGI_LOAD, text)
            self.assertEqual(dh.mod_wsgi(host)["state"], state, text)
        self.assertEqual(dh.mod_wsgi(FakeHost()), {"state": "absent"})

    def test_REGRESSION_an_empty_host_still_yields_one_bounded_document(self):
        code, stdout, stderr = run_main(FakeHost())
        report = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual({v["state"] for v in report["commands"].values()}, {"unavailable"})
        self.assertEqual(report["modWsgi"], {"state": "absent"})
        self.assertEqual(report["interpreters"], [])
        self.assertEqual(len(stderr.splitlines()), 2)


class Processes(Synthetic):
    def test_CONTROL_apache_and_its_daemons_are_identified_by_executable(self):
        report, _, _ = collect(uat_like_host())
        procs = {p["pid"]: p for p in report["processes"]["apache"]}
        self.assertEqual(procs[100], {"pid": 100, "executable": "/usr/sbin/apache2", "exeDeleted": False, "ppid": 1,
                                      "user": "root", "group": "root", "role": "server"})
        worker = procs[103]
        self.assertEqual((worker["role"], worker["cwd"], worker["mappedDirs"]), ("child", "/", ["/usr/lib/apache2/modules",
                                                                                              "/usr/lib/x86_64-linux-gnu"]))
        uat = procs[101]
        self.assertEqual((uat["role"], uat["wsgiGroup"], uat["titleTruncated"], uat["user"], uat["group"], uat["cwd"]),
                         ("wsgi-daemon", "dinify-uat", False, "www-data", "www-data", APP))
        self.assertIn("/usr/lib/x86_64-linux-gnu", uat["mappedDirs"])
        self.assertIn(VENV + "/lib/python3.12/site-packages/psycopg_binary", uat["mappedDirs"])
        self.assertEqual(uat["mappedUnsupported"], 1)
        admin = procs[102]                     # a daemon without display-name keeps Apache's title
        self.assertEqual((admin["role"], admin["exeDeleted"], admin["cwd"], admin["mappedDeleted"]), ("child", True, APP, 1))
        self.assertNotIn("wsgiGroup", admin)
        self.assertIn(VENV + "/lib/python3.12/site-packages/django", admin["mappedDirs"])     # the evidence it is one
        self.assertIn("/usr/lib/apache2/modules", admin["mappedDirs"])                        # Debian's mod_wsgi.so-3.12
        self.assertEqual(procs[104]["unsupported"], ["wsgiGroup"])
        self.assertNotIn("wsgiGroup", procs[104])
        self.assertEqual((report["processes"]["scanned"], report["processes"]["exeUnreadable"]), (11, 1))

    def test_CONTROL_a_truncated_title_names_a_prefix_of_its_group(self):
        host = uat_like_host()
        host.files["/proc/101/cmdline"] = b"(wsgi:dinify-cus\0"
        report, _, _ = collect(host)
        uat = next(p for p in report["processes"]["apache"] if p["pid"] == 101)
        self.assertEqual((uat["role"], uat["wsgiGroup"], uat["titleTruncated"]), ("wsgi-daemon", "dinify-cus", True))

    def test_CONTROL_a_title_alone_never_makes_a_process_apache(self):
        self.assertEqual(dh.parse_wsgi_title("(wsgi:dinify-uat)"), ("dinify-uat", False))
        self.assertEqual(dh.parse_wsgi_title("(wsgi:dinify-cust"), ("dinify-cust", True))
        self.assertIsNone(dh.parse_wsgi_title("/usr/sbin/apache2"))
        host = uat_like_host()
        host.links["/proc/202/exe"] = "/usr/sbin/apache2-lookalike"
        report, _, _ = collect(host)
        self.assertNotIn(202, [p["pid"] for p in report["processes"]["apache"]])


class PositiveControls(Synthetic):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(dh, "RealHost", _ForbiddenHost):
            cls.report, cls.stdout, cls.stderr = collect(uat_like_host())

    def daemon(self, name):
        return next(d for d in self.report["apache"]["directives"] if d.get("directive") == "WSGIDaemonProcess" and d.get("name") == name)

    def test_CONTROL_both_planes_wsgi_facts_are_retained(self):
        uat, admin = self.daemon("dinify-uat"), self.daemon("dinify-admin")
        self.assertEqual(uat["options"], {"user": "www-data", "group": "www-data", "processes": 2, "threads": 8,
                                          "display-name": "%{GROUP}", "python-home": VENV, "python-path": [APP], "home": APP,
                                          "shutdown-timeout": 20, "lang": "C.UTF-8", "locale": "C.UTF-8"})
        self.assertEqual((uat["state"], uat["vhost"], uat["conditional"]), ("parsed", 2, True))   # inside <IfModule mod_ssl.c>
        self.assertNotIn("conditional", admin)
        self.assertEqual((admin["options"]["processes"], admin["options"]["threads"], admin["options"]["shutdown-timeout"]), (1, 4, 15))
        mounts = {d["mount"]: d for d in self.report["apache"]["directives"] if d.get("directive") == "WSGIScriptAlias"}
        self.assertEqual(mounts["/uat"]["script"], APP + "/dinify_backend/wsgi.py")
        self.assertEqual(mounts["/uat"]["options"], {"process-group": "dinify-uat", "application-group": "%{GLOBAL}"})
        self.assertEqual(mounts["/api"]["options"]["process-group"], "dinify-admin")
        self.assertEqual([d["enabled"] for d in self.report["apache"]["directives"] if d.get("directive") == "WSGIPassAuthorization"],
                         [True, True])
        restrict = [d for d in self.report["apache"]["directives"] if d.get("directive") == "WSGIRestrictEmbedded"]
        self.assertEqual([d["enabled"] for d in restrict], [True])

    def test_CONTROL_static_media_and_vhost_facts_are_retained(self):
        aliases = {d["url"]: d.get("path") for d in self.report["apache"]["directives"] if d.get("directive") == "Alias" and "url" in d}
        self.assertEqual(aliases["/uat/static/"], APP + "/static/")
        self.assertEqual(aliases["/uat/media/"], MEDIA + "/")
        self.assertEqual(aliases["/api/static/"], APP + "/static/")
        names = {d.get("name") for d in self.report["apache"]["directives"] if d.get("directive") == "ServerName"}
        self.assertTrue({"api-test.dinifyapp.com", "admin.dinifyapp.com"} <= names)
        self.assertEqual([v["addresses"] for v in self.report["apache"]["vhosts"]], [["*:80"], ["*:443"], ["*:443"]])
        listens = [d.get("address") for d in self.report["apache"]["directives"] if d.get("directive") == "Listen"]
        self.assertEqual(listens, ["80", "443"])
        dump = self.report["commands"]["apache_vhosts"]
        self.assertEqual([(v["role"], v.get("serverName"), v.get("port")) for v in dump["vhosts"]],
                         [("default", "api-test.dinifyapp.com", None), ("namevhost", "api-test.dinifyapp.com", 443),
                          ("namevhost", "admin.dinifyapp.com", 443), ("only", "api-test.dinifyapp.com", None)])
        self.assertEqual(dump["vhosts"][1]["aliases"], ["www.api-test.dinifyapp.com"])
        self.assertEqual((dump["runUser"], dump["serverRoot"]), ({"id": 33, "name": "www-data"}, "/etc/apache2"))
        self.assertEqual(dump["unparsedLines"], 7)
        includes = self.report["commands"]["apache_includes"]
        self.assertTrue(includes["syntaxOk"])
        self.assertEqual(self.report["apache"]["includesNotScanned"], ["/etc/letsencrypt/options-ssl-apache.conf"])
        files = {f["path"]: f for f in self.report["apache"]["files"]}
        self.assertEqual({p for p, f in files.items() if not f["included"]}, {"/etc/apache2/sites-enabled/README"})
        linked = files["/etc/apache2/sites-enabled/api-test-le-ssl.conf"]
        self.assertEqual((linked["type"], linked["target"], linked["realpath"]),
                         ("link", "../sites-available/api-test-le-ssl.conf", "/etc/apache2/sites-available/api-test-le-ssl.conf"))
        self.assertEqual(linked["sha256"], hashlib.sha256(fill(CUSTOMER_SITE).encode()).hexdigest())

    def test_CONTROL_interpreter_and_mod_wsgi_facts_are_retained(self):
        interp = self.report["interpreters"][0]
        self.assertEqual((interp["path"], interp["realpath"], interp["state"]), ("/usr/bin/python3.12", "/usr/bin/python3.12", "ok"))
        self.assertEqual(interp["facts"], {k: v for k, v in INTERP_FACTS.items()})
        self.assertEqual((interp["libpython"], interp["libpythonFrom"]), (LIBPY, "INSTSONAME"))
        self.assertEqual(interp["libpythonSha256"], hashlib.sha256(b"\x7fELF synthetic libpython").hexdigest())
        self.assertEqual(self.report["interpreters"][1]["realpath"], "/usr/bin/python3.12")
        mw = self.report["modWsgi"]
        self.assertEqual((mw["state"], mw["module"], mw["realpath"], mw["moduleName"]), ("found", MOD, MOD, "wsgi_module"))
        self.assertEqual(mw["sha256"], hashlib.sha256(b"\x7fELF synthetic mod_wsgi").hexdigest())
        self.assertEqual((mw["needed"], mw["neededState"]), (["libpython3.12.so.1.0", "libc.so.6"], "ok"))
        cmds = self.report["commands"]
        self.assertEqual((cmds["apache_version"]["version"], cmds["apache_version"]["resolved"]), ("2.4.58", CTL))
        self.assertEqual(cmds["mod_wsgi_package"]["packages"],
                         [{"package": "libapache2-mod-wsgi-py3", "version": "5.0.0-1build2", "upstreamVersion": "5.0.0",
                           "status": "install ok installed"}])
        self.assertEqual(cmds["python_packages"]["packages"],
                         [{"package": "python3.12", "version": "3.12.3-1ubuntu0.8", "upstreamVersion": "3.12.3"},
                          {"package": "python3.12-venv", "version": None}])
        self.assertEqual((cmds["python_packages"]["state"], cmds["python_packages"]["exit"]), ("nonzero", 1))
        self.assertEqual(cmds["aws_cli"]["version"], "2.15.0")
        self.assertEqual([m["name"] for m in cmds["apache_modules"]["modules"]], ["core_module", "so_module", "wsgi_module", "ssl_module"])

    def test_CONTROL_ownership_and_key_presence_are_retained(self):
        trees = {t["path"]: t for t in self.report["projectTrees"]}
        self.assertEqual(sorted(trees), sorted([APP, APP + "/dinify_backend", APP + "/static", VENV, MEDIA]))
        app = trees[APP]
        self.assertEqual((app["gitCommit"], app["gitDetached"], app["owner"], app["mode"]), (COMMIT, True, "ubuntu", "755"))
        env = app["decoupleWouldFind"][0]
        self.assertEqual((env["path"], env["owner"], env["group"], env["mode"], env["type"]), (APP + "/.env", "ubuntu", "www-data", "640", "file"))
        self.assertEqual(env["keysPresent"], ["DATABASE_PASSWORD", "ENV", "MEDIA_ROOT", "SECRET_KEY"])
        self.assertEqual(env["exportOnly"], ["DINER_CAP_KEY"])        # decouple reads that key as "export DINER_CAP_KEY"
        self.assertEqual((env["envWord"], env["decoupleSelects"]), ("dev", True))
        self.assertEqual(set(env), {"path", "exists", "type", "mode", "owner", "group", "keysPresent", "keysAbsent", "envWord",
                                    "exportOnly", "decoupleSelects"})
        self.assertEqual(self.report["accounts"]["www-data"], {"uid": 33, "gid": 33, "groups": [], "home": "/var/www",
                                                               "shell": "/usr/sbin/nologin"})
        self.assertEqual(self.report["accounts"]["ubuntu"]["groups"], ["adm", "www-data"])
        self.assertIsNone(self.report["accounts"]["ssm-user"])
        self.assertEqual(self.report["locks"], ["/run/lock/dinify-host.lock"])
        self.assertEqual(self.report["host"], {"os": "Ubuntu 24.04.5 LTS", "kernel": "6.8.0-1009-aws", "machine": "x86_64", "euid": 0})

    def test_CONTROL_no_section_failed(self):
        self.assertNotIn("collector_error", self.stdout)


class EnvWord(Synthetic):
    def test_REGRESSION_ENV_is_one_of_four_words_and_nothing_else_is_printed(self):
        for line, word in (("ENV=dev", "dev"), ("ENV=test", "test"), ("ENV=prod", "prod"), ("ENV='prod'", "prod"),
                           ('export ENV="dev"', None), ("ENV=staging", "other"), ("ENV=Prod", "other"),
                           ("ENV=dev # local", "other"), ("ENV=%s" % C["env-word"], "other"), ("# ENV=prod", None),
                           ("ENV=dev\nENV=prod", "prod"), ("ENV=\"'dev'\"", "other"), (" ENV = prod ", "prod"),
                           ("SECRET_KEY=x\x0cENV=prod", None), ("ENV=test\rDEBUG=1", "test"), ("ENV", None)):
            doc = dh.parse_env_text(line + "\n")
            self.assertEqual(doc["envWord"], word, line)
            self.assertNoCanary(json.dumps(doc))
        self.assertNotIn("staging", json.dumps(dh.parse_env_text("ENV=staging\n")))

    def test_REGRESSION_an_unreadable_or_oversized_environment_file_is_a_state(self):
        host = uat_like_host()
        host.files[APP + "/.env"] = PermissionError(errno.EACCES, "denied")
        self.assertEqual(dh.env_file(host, APP + "/.env")["state"], "denied")
        host.files[APP + "/.env"] = b"SECRET_KEY=x\n" * (dh.READ_LIMIT // 8)
        doc = dh.env_file(host, APP + "/.env")
        self.assertEqual(doc["state"], "tooLarge")
        self.assertNotIn("keysPresent", doc)


class DecoupleSearch(Synthetic):
    def test_REGRESSION_the_file_decouple_would_read_is_marked_and_root_is_never_searched(self):
        host = uat_like_host()
        host.file("/home/ubuntu/.env", "ENV=prod\n")
        host.file("/.env", "ENV=prod\n")
        host.file(APP + "/dinify_backend/settings.ini", "[settings]\nENV = test\n")
        report, _, _ = collect(host)
        trees = {t["path"]: t for t in report["projectTrees"]}
        inner = [(e["path"], e.get("decoupleSelects"), e["envWord"]) for e in trees[APP + "/dinify_backend"]["decoupleWouldFind"]]
        self.assertEqual(inner, [(APP + "/dinify_backend/settings.ini", True, "test"), (APP + "/.env", None, "dev"),
                                 ("/home/ubuntu/.env", None, "prod")])
        self.assertNotIn("/.env", host.reads)

    def test_REGRESSION_a_linked_environment_file_is_read_through_its_link(self):
        host = uat_like_host()
        host.file("/etc/dinify/uat.env", "ENV=prod\nSECRET_KEY=%s\n" % C["symlink-env"], uid=0, gid=33, mode=0o640)
        del host.files[APP + "/.env"]
        host.link(APP + "/.env", "/etc/dinify/uat.env")
        report, stdout, _ = collect(host)
        env = next(t for t in report["projectTrees"] if t["path"] == APP)["decoupleWouldFind"][0]
        self.assertEqual((env["type"], env["envWord"], env["keysPresent"], env["decoupleSelects"]),
                         ("link", "prod", ["ENV", "SECRET_KEY"], True))
        self.assertEqual((env["resolvesTo"]["path"], env["resolvesTo"]["mode"], env["resolvesTo"]["group"]),
                         ("/etc/dinify/uat.env", "640", "www-data"))
        self.assertNoCanary(stdout, names=["symlink-env"])

    def test_REGRESSION_a_device_or_pipe_is_never_opened(self):
        host = uat_like_host()
        host.specials["/dev/zero"] = stat.S_IFCHR
        host.link("/etc/apache2/sites-enabled/zero.conf", "/dev/zero")
        host.specials[APP + "/.git/HEAD"] = stat.S_IFIFO
        del host.files[APP + "/.git/HEAD"]
        report, _, _ = collect(host)
        zero = next(f for f in report["apache"]["files"] if f["path"].endswith("zero.conf"))
        self.assertEqual((zero["state"], zero["sha256State"]), ("notRegular", "notRegular"))
        self.assertEqual(next(t for t in report["projectTrees"] if t["path"] == APP)["gitState"], "notRegular")
        self.assertEqual(host.violations, [])


class AgreesWithPythonDecouple(unittest.TestCase):
    """The collector's reading of an environment file against python-decouple 3.8 itself
    (a pinned requirement), on synthetic files written to a temporary directory."""

    ENV_CASES = ("ENV=dev\n", "ENV='prod'\nSECRET_KEY=x\n", 'ENV="\'dev\'"\n', "export ENV=dev\nDEBUG=1\n", "ENV=dev\r\nENV=test\r\n",
                 "ENV=test\rDEBUG=1\r", "SECRET_KEY=x\x0cENV=prod\n", "ENV=dev\x85DEBUG=1\n", "#ENV=prod\n  ENV = prod  \n",
                 "ENV=\n", "=dev\nENV\n", "ENV=dev # c\n", "\ufeffENV=dev\n", "ENV=prod\nENV=dev\n", "ENV='dev\n")
    INI_CASES = ("[settings]\nENV=prod\n", "[settings]\nenv: dev\nSECRET_KEY = x\n", "[other]\nENV=prod\n",
                 "[settings]\nENV=%(missing)s\n", "ENV=dev\n", "[settings]\nENV=a\nENV=b\n", "[settings]\nENV = 'dev'\n")

    def test_CONTRACT_a_dotenv_is_read_exactly_as_RepositoryEnv_reads_it(self):
        decouple = importlib.import_module("decouple")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            for text in self.ENV_CASES:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                data = decouple.RepositoryEnv(path).data
                want = (sorted(k for k in dh.ENV_KEYS if k in data),
                        None if "ENV" not in data else data["ENV"] if data["ENV"] in dh.ENV_WORDS else "other")
                got = dh.parse_env_text(text)
                self.assertEqual((got["keysPresent"], got["envWord"]), want, repr(text))

    def test_CONTRACT_a_settings_ini_is_read_exactly_as_RepositoryIni_reads_it(self):
        decouple = importlib.import_module("decouple")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.ini")
            for text in self.INI_CASES:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                got = dh.parse_settings_ini_text(text)
                try:
                    repo = decouple.RepositoryIni(path)
                except Exception:
                    self.assertEqual(got, {"state": "unparseable"}, repr(text))
                    continue
                keys = sorted(k for k in dh.ENV_KEYS if repo.parser.has_option(repo.SECTION, k))
                try:
                    env = repo["ENV"] if "ENV" in keys else None
                except Exception:
                    env = "unreadable"
                want_env = None if env is None else env if env in dh.ENV_WORDS else "other"
                self.assertEqual((got["keysPresent"], got["envWord"]), (keys, want_env), repr(text))


class ProfileFactMap(Synthetic):
    """Every field the profile needs is either read from this report or a named decision."""

    @staticmethod
    def leaves(doc, prefix=""):
        for key, value in doc.items():
            path = prefix + key
            if key.startswith("_") or path in ("schema", "kind", "status", "observation"):
                continue
            if isinstance(value, dict):
                yield from ProfileFactMap.leaves(value, path + ".")
            else:
                yield path

    @staticmethod
    def resolve(doc, location):
        """``a.b[].c``: every value at that path; ``b[k=v]`` keeps the list items whose k is v."""
        nodes = [doc]
        for part in re.findall(r"[^.\[]+(?:\[[^\]]*\])?", location):
            m = re.fullmatch(r"([^\[]+)(?:\[([^\]=]*)(?:=([^\]]*))?\])?", part)
            key, field, value = m.group(1), m.group(2), m.group(3)
            nodes = [n.get(key) for n in nodes if isinstance(n, dict)]
            if field is not None:
                nodes = [item for n in nodes if isinstance(n, list) for item in n
                         if not field or isinstance(item, dict) and str(item.get(field)) == value]
        return [n for n in nodes if n is not None]

    def test_CONTROL_a_qualified_location_keeps_only_that_directive(self):
        report, _, _ = collect(uat_like_host())
        self.assertEqual(self.resolve(report, "apache.directives[directive=WSGIScriptAlias].mount"), ["/api", "/uat"])
        self.assertEqual(self.resolve(report, "apache.directives[directive=WSGIPassAuthorization].enabled"), [True, True])

    def test_CONTRACT_every_profile_field_is_mapped(self):
        with open(PROFILE, encoding="utf-8") as fh:
            profile = json.load(fh)
        fields = {p.replace("planes.customer.", "planes.*.").replace("planes.admin.", "planes.*.") for p in self.leaves(profile)}
        self.assertEqual(fields, set(dh.PROFILE_FACTS))
        for field, (kind, where) in dh.PROFILE_FACTS.items():
            self.assertIn(kind, ("report", "decision", "gap"), field)
            self.assertTrue(where, field)

    def test_CONTRACT_every_report_location_holds_a_value_in_a_synthetic_report(self):
        report, _, _ = collect(uat_like_host())
        for field, (kind, where) in dh.PROFILE_FACTS.items():
            if kind == "report":
                self.assertTrue(self.resolve(report, where), "%s: nothing at %s" % (field, where))


    def test_CONTRACT_a_report_fact_is_acceptable_to_the_profile_as_observed(self):
        """A "report" field must be copyable as is: at least one value at its location passes
        release/hostprofile.py's own grammar for that field. A value that needs reshaping first
        (a Debian revision, a URL composed from parts) is a decision, not an observation."""
        report, _, _ = collect(uat_like_host())
        with open(PROFILE, encoding="utf-8") as fh:
            base = json.load(fh)
        for field, (kind, where) in dh.PROFILE_FACTS.items():
            if kind != "report":
                continue
            path = field.replace("planes.*.", "planes.customer.").split(".")
            accepted = []
            for value in self.resolve(report, where):
                doc = copy.deepcopy(base)
                node = doc
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                invalid = [p for p in hp.validate(doc) if p["code"] == "profile_invalid"
                           and p["detail"].startswith(".".join(path) + " ")]
                accepted.append(not invalid)
            self.assertTrue(any(accepted), "%s: no value at %s is a valid profile value" % (field, where))


class MainIntegration(Synthetic):
    def test_REGRESSION_main_emits_one_document_and_the_two_digests_and_nothing_else(self):
        host = uat_like_host()
        code, stdout, stderr = run_main(host)
        self.assertEqual(code, 0)
        self.assertTrue(stdout.endswith("}\n"))
        report = json.loads(stdout)                    # one document: trailing data would not parse
        self.assertEqual(report["schema"], "dinify.backend.host-discovery/2")
        self.assertEqual(report["collectedAt"], "2026-10-15T09:00:00Z")
        with open(COLLECTOR, "rb") as fh:
            collector = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(stderr.splitlines(), ["collectorSha256=%s" % collector,
                                               "reportSha256=%s" % hashlib.sha256(stdout.encode()).hexdigest()])
        self.assertEqual(dh.collector_sha256(), collector)
        self.assertNoCanary(stdout, stderr)
        self.assertEqual(host.violations, [])
        expected = {tuple(host.which(v[0]) and [host.which(v[0])] + v[1:]) for v in dh.READ_ONLY_COMMANDS.values()}
        expected |= {_cmd("/usr/bin/python3.12", *INTERP_ARGS), _cmd("/usr/bin/python3", *INTERP_ARGS),
                     _cmd("/usr/bin/readelf", "-d", MOD)}
        self.assertEqual(set(host.ran), expected)
        self.assertTrue(stdout.isascii())


class RealHostRunner(unittest.TestCase):
    """The real runner, driven ONLY against fixture scripts written here: it keeps stdout and
    stderr apart, and a fact built from it carries neither stream's text."""

    def fixture(self, d, name, body):
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_REGRESSION_stdout_and_stderr_stay_apart_and_stderr_text_is_never_kept(self):
        with tempfile.TemporaryDirectory() as d:
            script = self.fixture(d, "fake-ctl", "echo 'Server version: Apache/2.4.58 (Ubuntu)'\necho '%s' >&2\nexit 3\n"
                                  % C["stderr-v"])
            host = REAL_HOST()
            result = host.run([script])
            self.assertNotIn(C["stderr-v"], result["stdout"])
            self.assertIn(C["stderr-v"], result["stderr"])
            fact = dh.command_fact(host, [script], dh.parse_apache_version)
        self.assertEqual({k: fact[k] for k in ("state", "exit", "stderr", "version")},
                         {"state": "nonzero", "exit": 3, "stderr": "present", "version": "2.4.58"})
        self.assertNotIn(C["stderr-v"], json.dumps(fact))

    def test_REGRESSION_timeout_and_absence_are_fixed_states(self):
        with tempfile.TemporaryDirectory() as d:
            script = self.fixture(d, "slow", "exec sleep 5\n")
            self.assertEqual(REAL_HOST().run([script], timeout=1)["state"], "timeout")
            missing = os.path.join(d, "absent")
            self.assertIsNone(REAL_HOST().which(missing))
            self.assertEqual(REAL_HOST().run([missing])["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
