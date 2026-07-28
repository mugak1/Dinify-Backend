"""
Deploy check: a deployed environment must run on PostgreSQL (DB-VENDOR-00).

WHY THIS EXISTS. ``restaurants_app.controllers.admission_lock._lock`` is the
synchronisation primitive that lets order admission and a lifecycle transition agree
about a restaurant's state, and it is implemented with a PostgreSQL advisory lock. On
any other backend it RETURNS SILENTLY — deliberately, because the unit suite runs on
SQLite where advisory locks do not exist. That no-op is correct for a test run and
catastrophic in production: order admission would stop being synchronised with
``transition_restaurant``, ``Order.is_test`` could be derived from a status that had
already changed, and there would be no error, no log line and no failing test. The
mechanism would simply not be there.

Nothing else notices. ``CONN_MAX_AGE``, the migrations and the ORM all work on other
backends; the only casualty is a safety property whose absence is invisible. So the
configuration is asserted where a wrong answer is still cheap to fix — at deploy time,
before Apache is restarted.

THE PREDICATE IS DELIBERATELY ``connection.vendor``, NOT the ``ENGINE`` string. It has
to be the SAME question ``_lock`` asks when it decides to no-op
(``admission_lock.py``: ``if connection.vendor != 'postgresql': return``). An ENGINE
comparison would answer a different one: a wrapper backend such as
``django_prometheus.db.backends.postgresql`` reports ``vendor == 'postgresql'`` and
holds real advisory locks, so it must pass — and an ENGINE check would fail it.
Whenever the two could disagree, this check is the one that would be wrong.

GATING. Silent whenever ``DEBUG`` is true, which is what keeps it out of ordinary local
development and CI: ``dinify_backend/test_settings.py`` sets ``DEBUG=True`` and falls
back to SQLite in-memory when no ``DATABASE_*`` is exported. ``DEBUG`` is the house
production discriminator — ``settings.py`` reads ``config('DEBUG', default=False)``, so
it fails closed — and this mirrors the inversion in
``dinify_backend/diner_cap_config.py::resolve_diner_cap_key``, which permits a derived
development key under ``debug`` and raises without it. ``ENV`` is deliberately NOT used:
it is not a Django setting at all, it is read ad hoc through ``decouple.config`` at two
call sites, and no code validates that its value is one of the three it is assumed to
take.

WHERE IT ACTUALLY RUNS. ``check --deploy`` is invoked by ``deploy-uat.yml`` twice —
once as ``ubuntu`` and once as ``www-data`` to reproduce what mod_wsgi sees — both
before ``systemctl restart apache2`` and under ``set -e``, so an Error here aborts the
deploy with the old workers still serving. It is NOT invoked by ``ci.yml`` or
``scripts/verify.sh``; ``restaurants_app/tests_db_vendor_check.py`` exercises the
predicate directly instead, which is why that predicate takes plain arguments rather
than reading ``django.conf.settings`` — the same testability seam
``resolve_diner_cap_key`` uses.
"""
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import DEFAULT_DB_ALIAS, connections

# The one backend on which the advisory-lock primitive is real. Compared against
# ``BaseDatabaseWrapper.vendor``, not against a dotted ENGINE path — see the module
# docstring for why that distinction is load-bearing rather than stylistic.
REQUIRED_DB_VENDOR = 'postgresql'

CHECK_ID = 'dinify.E001'


def check_production_database_vendor(vendor, debug):
    """
    Return the check messages for a database ``vendor`` under ``debug``.

    Pure: no settings, no connection, no queries — so the deploy check's actual
    decision can be exercised directly by tests rather than only through a
    production-shaped environment. Callers pass what they observed.
    """
    if debug:
        return []
    if vendor == REQUIRED_DB_VENDOR:
        return []
    return [
        Error(
            'A deployed Dinify backend must run on PostgreSQL; the default '
            'database reports vendor {!r}.'.format(vendor),
            hint=(
                'Order admission is synchronised with restaurant lifecycle '
                'transitions by a PostgreSQL advisory lock '
                '(restaurants_app/controllers/admission_lock.py). On any other '
                'backend that primitive returns silently, so orders would be '
                'admitted — and classified commercial or rehearsal — against a '
                'status a concurrent transition may already have changed, with no '
                'error raised anywhere. Set DATABASE_ENGINE to a PostgreSQL '
                'backend, or set DEBUG=True if this really is a development '
                'environment.'
            ),
            id=CHECK_ID,
        )
    ]


@register(Tags.database, deploy=True)
def production_requires_postgresql(app_configs, **kwargs):
    """
    Wire the pure predicate to the live configuration.

    Reading ``.vendor`` instantiates the backend wrapper but does not open a
    connection, so this stays safe to run against an unreachable database — which is
    the state a misconfigured deploy is most likely to be in.
    """
    return check_production_database_vendor(
        connections[DEFAULT_DB_ALIAS].vendor, settings.DEBUG,
    )
