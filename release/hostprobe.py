"""HOST PROBE (D08 B3) — run by an INSTALLED release's own interpreter, as an unprivileged
identity, from the TRUSTED verifier's checkout, never from the release:

    <release>/venv/bin/python -I <trusted>/release/hostprobe.py config|migrations|migrate  < request.json

It imports the release's code with the plane's REAL configuration (that is its purpose, and
why it never runs as root), answers one JSON document on stdout, and changes nothing except
in ``migrate`` mode, which applies exactly the pending migrations it was told to expect.

``config``      is this release, with this plane's configuration, fit to serve? The file's
                ownership and mode; the settings import; DEBUG; the ENV value against the
                profile's deterministic-OTP decision; the admin key's structure (admin plane);
                provider settings only where the configured ENV enables the provider; the
                deployment system checks (ERROR fails, warnings are listed by id); one
                ``SELECT 1``; where media and static files resolve.
``migrations``  the ACTUAL plan against the configured database: pending migrations with
                their file digests and operation types, migrations the database has applied
                that this release does not know, and graph conflicts. Read-only.
``migrate``     apply the plan, but only if it is exactly the one the transition reviewed.

NEVER EMITTED: a configuration value (other than ENV's word from a fixed vocabulary and
DEBUG's boolean), a key, a password, a connection string, a host name, an exception message
that might carry any of those. Failures are reported by exception TYPE and a fixed sentence.
No message is sent: no SMS, no email, no OTP, no payment request, no outbound call.
"""

import hashlib
import inspect
import json
import os
import stat
import sys

ENV_WORDS = ("dev", "test", "prod")
# Operation types that only ADD schema an older release can ignore. Anything else needs a
# reviewed decision naming it; a destructive type contradicts an "expand" decision outright.
ADDITIVE = ("CreateModel", "AddField", "AddIndex", "AddIndexConcurrently", "AddConstraint", "AlterModelOptions",
            "AlterModelManagers", "AlterModelTable", "AlterModelTableComment")
DESTRUCTIVE = ("DeleteModel", "RemoveField", "RenameField", "RenameModel", "RemoveIndex", "RemoveIndexConcurrently",
               "RemoveConstraint", "AlterUniqueTogether", "AlterIndexTogether", "RenameIndex")


def _problem(code, detail):
    return {"code": code, "detail": detail}


def _setup(release, plane):
    """Load the plane's configuration the way its launcher does, then Django. Returns problems."""
    sys.path[:0] = [os.path.join(release, "wsgi"), os.path.join(release, "source")]
    import dinify_release_runtime as runtime
    try:
        runtime.load_config(plane)
    except (OSError, UnicodeDecodeError) as error:
        return [_problem("config_unreadable", "the %s configuration cannot be read by this identity (%s)" % (plane, type(error).__name__))]
    try:
        import django
        django.setup()
        from django.conf import settings
        settings.INSTALLED_APPS   # force the settings import
    except Exception as error:   # the settings' own fail-closed checks land here; never echo their text
        return [_problem("settings_failed", "the %s settings did not load under this configuration (%s)" % (plane, type(error).__name__))]
    return []


def config(request):
    release, plane = request["release"], request["plane"]
    out = {"plane": plane}
    problems = []
    path = request["configPath"]
    try:
        st = os.stat(path)
        if st.st_mode & 0o027 or os.access(path, os.W_OK):
            problems.append(_problem("config_permissions", "the %s configuration must be unwritable by the runtime identity and "
                                     "carry no group-write and no permission for others (mode %o)" % (plane, stat.S_IMODE(st.st_mode))))
    except OSError as error:
        return dict(out, problems=[_problem("config_unreadable", "the %s configuration cannot be inspected (%s)" % (plane, type(error).__name__))])
    setup = _setup(release, plane)
    if setup:
        return dict(out, problems=problems + setup)
    from django.conf import settings
    from decouple import config as cfg
    out["debug"] = bool(settings.DEBUG)
    if settings.DEBUG:
        problems.append(_problem("debug_enabled", "DEBUG is on for the %s plane" % plane))
    env = cfg("ENV", default=None)
    out["env"] = env if env in ENV_WORDS else ("unset" if env is None else "unrecognised")
    if env not in ENV_WORDS:
        problems.append(_problem("env_invalid", "ENV must be one of %s (it decides OTP and SMS behaviour)" % ", ".join(ENV_WORDS)))
    elif env == "dev" and not request["otpAllowed"]:
        problems.append(_problem("deterministic_otp_refused", "ENV=dev makes every OTP 1234 and sends no SMS; the profile does not "
                                 "allow the deterministic test OTP on this host"))
    if env in ("test", "prod"):
        missing = [k for k in ("YO_SMS_ACCOUNT_NO", "YO_SMS_PASSWORD") if not cfg(k, default="")]
        if missing:
            problems.append(_problem("provider_config_missing", "ENV=%s sends SMS and needs %s" % (env, ", ".join(missing))))
    if plane == "admin":
        key = cfg("ADMIN_SECRET_ENCRYPTION_KEY", default="")
        try:
            from cryptography.fernet import Fernet
            Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            problems.append(_problem("admin_key_invalid", "ADMIN_SECRET_ENCRYPTION_KEY is missing or not a Fernet key; TOTP sign-in would fail"))
    from django.core import checks
    found = checks.run_checks(include_deployment_checks=True)
    errors = sorted({m.id or "unidentified" for m in found if m.level >= checks.ERROR and not m.is_silenced()})
    out["checks"] = {"errors": errors, "warnings": sorted({m.id or "unidentified" for m in found if m.level < checks.ERROR and not m.is_silenced()})}
    if errors:
        problems.append(_problem("deploy_checks_failed", "system checks at ERROR: %s" % ", ".join(errors)))
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        out["database"] = "connected"
    except Exception as error:
        out["database"] = "unreachable"
        problems.append(_problem("database_unreachable", "SELECT 1 failed (%s)" % type(error).__name__))
    media = os.path.realpath(str(settings.MEDIA_ROOT))
    if media.rstrip("/") != request["mediaRoot"].rstrip("/"):
        problems.append(_problem("media_root_mismatch", "MEDIA_ROOT does not resolve to the profile's media root"))
    elif (media + "/").startswith(os.path.dirname(release) + "/"):
        problems.append(_problem("media_root_mismatch", "MEDIA_ROOT is inside the release root"))
    elif not (os.path.isdir(media) and os.access(media, os.W_OK)):
        problems.append(_problem("media_root_unwritable", "the media root is not a directory this identity can write"))
    if os.path.realpath(str(settings.STATIC_ROOT)) != os.path.join(release, "static"):
        problems.append(_problem("static_root_mismatch", "STATIC_ROOT is not this release's static directory"))
    return dict(out, problems=problems)


def _operations(migration):
    names = []
    for op in migration.operations:
        names.append(type(op).__name__)
        for inner in list(getattr(op, "database_operations", []) or []) + list(getattr(op, "state_operations", []) or []):
            names.append("%s.%s" % (type(op).__name__, type(inner).__name__))
    return names


def _file_sha(migration):
    path = inspect.getsourcefile(type(migration)) or ""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def plan():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    loader = executor.loader
    conflicts = sorted("%s: %s" % (app, ", ".join(sorted(names))) for app, names in loader.detect_conflicts().items())
    targets = loader.graph.leaf_nodes()
    steps = executor.migration_plan(targets)
    pending = [{"id": "%s.%s" % (m.app_label, m.name), "sha256": _file_sha(m), "operations": _operations(m), "backwards": bool(back)}
               for m, back in steps]
    unknown = sorted("%s.%s" % key for key in loader.applied_migrations if key not in loader.disk_migrations)
    return {"pending": pending, "appliedUnknown": unknown, "conflicts": conflicts}


def migrations(request):
    setup = _setup(request["release"], "customer")
    if setup:
        return {"problems": setup}
    try:
        return dict(plan(), problems=[])
    except Exception as error:
        return {"problems": [_problem("migration_plan_failed", "the migration plan could not be read (%s)" % type(error).__name__)]}


def migrate(request):
    setup = _setup(request["release"], "customer")
    if setup:
        return {"problems": setup}
    before = plan()
    if [p["id"] for p in before["pending"]] != request["expect"] or before["appliedUnknown"] or before["conflicts"]:
        return {"problems": [_problem("migration_plan_changed", "the plan is not the reviewed one; nothing was applied")], "before": before}
    from django.core.management import call_command
    try:
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as error:
        return {"problems": [_problem("migration_failed", "migrate raised %s; the schema state must be inspected before anything "
                                      "else runs" % type(error).__name__)], "before": before}
    after = plan()
    problems = [] if not after["pending"] else [_problem("migration_incomplete", "migrations remain pending after migrate")]
    return {"problems": problems, "applied": [p["id"] for p in before["pending"]], "after": after}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    handlers = {"config": config, "migrations": migrations, "migrate": migrate}
    if mode not in handlers:
        print(json.dumps({"problems": [_problem("usage", "mode is one of %s" % ", ".join(sorted(handlers)))]}))
        return 64
    request = json.loads(sys.stdin.read())
    try:
        answer = handlers[mode](request)
    except Exception as error:
        answer = {"problems": [_problem("probe_failed", "the %s probe failed (%s)" % (mode, type(error).__name__))]}
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
