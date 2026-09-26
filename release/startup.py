"""BOUNDED STARTUP — executed by a RECONSTRUCTED environment's interpreter
(``<venv>/bin/python -I startup.py --source DIR --scratch DIR --imports a,b,...``), from the
consumer's own checkout, never from the candidate under examination.

What it does, in two fresh processes (Django settings cannot be swapped in one):

  customer  ``dinify_backend.test_settings`` — the repository's own disposable settings:
            SQLite in memory, the locmem email backend, MongoDB stubbed, test-only keys —
            then ``django.setup()``, the full system check, the customer WSGI entry module
            (``dinify_backend.wsgi``) and one in-process WSGI request to
            ``/api/v1/health/``, which runs ``SELECT 1`` against that in-memory database;
            then every top-level module the installed wheels provide is imported, and the
            native wheels are exercised (psycopg's binary libpq, Pillow, cryptography,
            qrcode).
  admin     the admin plane's real settings module (``dinify_backend.settings_admin``)
            layered over the same disposable base by a settings module written into the
            scratch directory — never into the source — then the system check, the admin
            WSGI entry module (``dinify_backend.wsgi_admin``) and one request to
            ``/admin/v1/health/`` on the admin host.

No real environment file is read (the consumer refuses to start if one exists anywhere a
settings lookup could climb to), no network is reachable (a dead proxy is set), no OTP or
email is sent, no deployed database is contacted.

WHAT THIS DOES NOT PROVE, stated so none of it is inferred: anything about Apache, the
mod_wsgi module or the interpreter it embeds on the host, the host's shared libraries or
filesystem layout, its real ``.env``, PostgreSQL or MongoDB connectivity, TLS, or the
``/uat`` / ``/api`` mount prefixes. It proves the retained inputs rebuild an environment in
which both planes' settings and WSGI callables load and answer one request.
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
import traceback

ADMIN_SETTINGS = '''"""Written by release/startup.py for one reconstruction; not part of the source."""
import dinify_backend.test_settings as _disposable      # test-only keys, SQLite, locmem email, MongoDB stub
from dinify_backend.settings_admin import *             # noqa: F401,F403 — the admin plane itself
DATABASES = _disposable.DATABASES
EMAIL_BACKEND = _disposable.EMAIL_BACKEND
PASSWORD_HASHERS = _disposable.PASSWORD_HASHERS
'''


def wsgi_get(application, path, host):
    status, headers = {}, {}

    def start_response(value, response_headers, exc_info=None):
        status["value"] = value
        headers.update(dict(response_headers))

    environ = {
        "REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "", "SERVER_NAME": host, "SERVER_PORT": "443",
        "HTTP_HOST": host, "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.version": (1, 0), "wsgi.url_scheme": "https",
        "wsgi.input": __import__("io").BytesIO(b""), "wsgi.errors": sys.stderr, "wsgi.multithread": False,
        "wsgi.multiprocess": False, "wsgi.run_once": True, "REMOTE_ADDR": "127.0.0.1",
    }
    body = b"".join(application(environ, start_response))
    return status.get("value", ""), body


def _setup(source, settings):
    sys.path.insert(0, source)
    os.environ["DJANGO_SETTINGS_MODULE"] = settings
    import django
    from django.core.management import call_command
    django.setup()
    # Raises SystemCheckError on any error-level finding; its report goes to stderr so
    # stdout carries only this script's JSON answer.
    call_command("check", stdout=sys.stderr, stderr=sys.stderr)


def customer(source, imports):
    _setup(source, "dinify_backend.test_settings")
    application = importlib.import_module("dinify_backend.wsgi").application
    status, body = wsgi_get(application, "/api/v1/health/", "localhost")
    health = json.loads(body.decode("utf-8"))
    if not status.startswith("200") or health.get("status") != "ok" or health.get("database") != "connected":
        raise AssertionError("customer health answered %s %s" % (status, body[:200]))
    imported = []
    for name in imports:
        importlib.import_module(name)
        imported.append(name)
    import psycopg
    if psycopg.pq.__impl__ != "binary":
        raise AssertionError("psycopg is using the %s libpq implementation, not the locked binary wheel" % psycopg.pq.__impl__)
    from PIL import Image
    Image.new("RGB", (2, 2)).convert("L")
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    if Fernet(key).decrypt(Fernet(key).encrypt(b"x")) != b"x":
        raise AssertionError("cryptography round trip failed")
    import qrcode
    qrcode.make("dinify").size
    return {"settings": "dinify_backend.test_settings", "wsgi": "dinify_backend.wsgi", "health": health["status"],
            "database": health["database"], "imported": imported, "libpq": psycopg.pq.__impl__}


def admin(source, scratch):
    settings_dir = os.path.join(scratch, "settings")
    os.makedirs(settings_dir, exist_ok=True)
    with open(os.path.join(settings_dir, "reconstruct_admin_settings.py"), "w", encoding="utf-8") as fh:
        fh.write(ADMIN_SETTINGS)
    sys.path.insert(0, settings_dir)
    _setup(source, "reconstruct_admin_settings")
    from django.conf import settings
    if settings.ROOT_URLCONF != "dinify_backend.urls_admin":
        raise AssertionError("the admin settings did not load the admin plane")
    application = importlib.import_module("dinify_backend.wsgi_admin").application
    host = settings.ALLOWED_HOSTS[0]
    status, body = wsgi_get(application, "/admin/v1/health/", host)
    health = json.loads(body.decode("utf-8"))
    if not status.startswith("200") or health.get("status") != "ok":
        raise AssertionError("admin health answered %s %s" % (status, body[:200]))
    return {"settings": "dinify_backend.settings_admin (over the disposable base)", "wsgi": "dinify_backend.wsgi_admin",
            "urlconf": settings.ROOT_URLCONF, "host": host, "health": health["status"]}


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--imports", default="")
    parser.add_argument("--plane", choices=("customer", "admin"))
    args = parser.parse_args(argv)
    imports = [n for n in args.imports.split(",") if n]
    if args.plane:
        answer_stream, sys.stdout = sys.stdout, sys.stderr  # nothing the application prints can corrupt the answer
        try:
            answer = customer(args.source, imports) if args.plane == "customer" else admin(args.source, args.scratch)
            answer_stream.write(json.dumps({"ok": True, "plane": args.plane, "detail": answer}))
            return 0
        except Exception:  # reported, never swallowed: the parent fails the reconstruction
            answer_stream.write(json.dumps({"ok": False, "plane": args.plane, "error": traceback.format_exc()[-2500:]}))
            return 1
    results, failures = {}, []
    for plane in ("customer", "admin"):
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--source", args.source, "--scratch", args.scratch,
                               "--imports", args.imports, "--plane", plane], capture_output=True, text=True, timeout=300, check=False)
        try:
            answer = json.loads(proc.stdout)
        except ValueError:
            answer = {"ok": False, "plane": plane, "error": (proc.stderr or proc.stdout)[-2500:]}
        results[plane] = answer
        if proc.returncode != 0 or not answer.get("ok"):
            failures.append({"plane": plane, "error": answer.get("error")})
    sys.stdout.write(json.dumps({"ok": not failures, "planes": results, "failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
