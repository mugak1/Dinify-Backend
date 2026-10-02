"""D15 R3 — the restore-usability check: bounded, read-only checks of a RESTORED backend.

This answers ONE question about a database and media root that have already been
restored somewhere isolated: do the restored data and THIS revision of the code serve
the customer/application reads consistently, and — where independent evidence is
supplied — does the restore agree with history recorded outside it? It is not an Admin
recovery suite, a backup tool or a cutover tool. It CAPTURES nothing, RESTORES nothing,
REPAIRS nothing, MIGRATES nothing and WRITES no application data; the database role it
requires refuses writes on its own, and a read that tries one fails.

NOTHING RUNS IT AUTOMATICALLY. No workflow, deploy step, schedule or signal invokes this
module, and merging or deploying this repository is not permission to run it against
restored or live data. An owner runs it, by hand, against an isolated target they
approved.

SUPPORTED PROFILE (the only one)
================================
An owner-approved, isolated SOURCE CHECKOUT of this repository pinned to one full
40-character revision, run by the interpreter and packages ``release/python-lock.json``
names, against a restored PostgreSQL database reached through a Unix socket and a
restored media root mounted read-only at the path the settings name. A ``.git``-less
installed release, a B3 serving identity and production recovery are NOT supported and
nothing here claims they are.

INVOCATION
==========
From the checkout root, in a process whose network namespace has no interface up and
no route (Unix sockets still work)::

    DJANGO_SETTINGS_MODULE=dinify_backend.settings \\
    python -m misc_app.restore_usability --inputs INPUTS.json \\
        --result RESULT.json --nonce <16-64 lowercase hex>

``--result`` must not exist; it is published atomically, never overwritten, and only
when the run ends. Its ``nonce`` echoes ``--nonce`` so a stale file cannot be read as
this run's answer. ``python -m misc_app.restore_usability --help`` prints this summary.

``manage.py check_restore_usability`` is the thin ADAPTER this bootstrap hands over to.
Invoked directly it REFUSES (exit 3): ``manage.py`` imports the settings and
initialises Django before any command code runs, so a direct invocation could only
check its conditions AFTER the application had started — that is not pre-start
protection, and the adapter never presents it as such. An environment variable is not
accepted as proof that the bootstrap ran: the handover is an in-process object the
bootstrap creates after its pre-start phase.

ORDER IS THE CONTRACT
=====================
1. **Pre-start, standard library only** — nothing from Django, the settings module or
   any application package has been imported (the entry check proves it):
   ``P.inputs`` (the inputs and manifest documents), ``P.entrypoint``,
   ``P.source_identity``, ``P.runtime``, ``P.outbound_denied``, ``P.media_readonly``
   and ``P.attestations``. Then two configured controls are established: the Mongo
   connection target is set to an unparseable URI, so the application's own lazy
   client fails inside its constructor (no socket, no thread), and
   ``DJANGO_SETTINGS_MODULE`` is fixed to the inputs' value.
2. **Settings imported, Django NOT set up**: ``P.target_identity`` and
   ``P.signing_keys_present``; the e-mail backend is replaced IN THIS PROCESS by
   Django's in-memory backend (no settings file is edited).
3. **Django set up, before any read**, inside the adapter: ``P.providers_inert`` (both
   controls verified on the live objects, not on the flags), ``P.db_readonly`` and
   ``P.db_exclusive``.
4. Reads and checks.

A precondition that is not satisfied — or cannot be evaluated — REFUSES the run
(exit 3); nothing after it runs. ``P.attestations`` is the one precondition that is
ATTESTED rather than observed: the operator states that no live traffic is routed to
the target (``no_live_routing``), that nothing scheduled or background runs against it
(``no_background_work``), and that the configuration and credentials are in their
custody (``configuration_custody``). Those three cannot be observed from inside this
process and the result never calls them PASS.

INPUTS — one document, schema ``dinify.restore-usability.inputs/1``
=================================================================
Unknown keys are refused at every level, and so is any malformed, empty or incomplete
SUPPLIED evidence; an OMITTED optional section is simply unavailable::

    {"schema": "dinify.restore-usability.inputs/1",
     "release":  {"source_revision": "<40 lowercase hex>"},                 REQUIRED
     "target":   {"settings_module": "dinify_backend.settings",             REQUIRED
                  "database": {"name", "user", "host": "<absolute socket dir>",
                               "port": "<digits>"},
                  "media_root": "<absolute path, read-only mount>"},
     "limits":   {"wall_clock_seconds": <int >= 1>,                         REQUIRED
                  "max_restaurants": 10, "max_orders_per_restaurant": 20,
                  "max_sections_per_restaurant": 50, "max_media_objects": 500},
     "attestations": {"no_live_routing": true, "no_background_work": true,  REQUIRED
                      "configuration_custody": true, "attested_by": "<who>"},
     "sample":   {"restaurant_ids": ["<uuid>", ...]},       optional; not with a manifest
     "staff_principal": {"user_id": "<uuid>"},              optional
     "originals": {                                         optional; at least one kind
         "printed_qr": [{"table_id", "restaurant_id", "credential"}, ...],
         "orders": [{"order_id", "restaurant_id", "table_id",
                     "state": "accepted"|"exists", "quote_ref" (iff accepted)}, ...],
         "staff_bearer_token": "<access token issued BEFORE the recovery point>"},
     "manifest": {"path": "<absolute>", "sha256": "<64 hex of its bytes>"},  optional
     "require":  ["usable"]}                                optional (default shown)

``wall_clock_seconds`` is the operator's bound for THIS run. It is not an RTO and this
module invents no production time limit. It cannot start before the inputs are read, so
reading them has a FIXED bound of its own: the inputs phase is interrupted after
``INPUTS_PHASE_SECONDS``, and both documents must be REGULAR files no larger than
``MAX_INPUTS_BYTES`` and ``MAX_MANIFEST_BYTES`` — opened without blocking (a FIFO with no
writer would otherwise wait for one indefinitely) and refused unread otherwise. A run's
whole bound is therefore ``INPUTS_PHASE_SECONDS`` plus ``wall_clock_seconds``, plus
cleanup. ``max_restaurants`` and
``max_orders_per_restaurant`` bound the SAMPLE: every ``I.`` check describes the sampled
restaurants and orders, never the whole restore, and ``result.sample.not_sampled`` says
how much was left out. Where a bound cuts a check's OWN coverage
(``max_sections_per_restaurant``, ``max_media_objects``), that check is INCOMPLETE, never
silently truncated into a PASS.

Originals must predate the recovery point; this check cannot know when they were made.
They are BEARER CREDENTIALS where noted and are never written to the result, a log or
stdout: the result carries table ids, counts and a redacted digest only.

The MANIFEST (schema ``dinify.restore-usability.manifest/1``) is a backup-time record
bound to the recovery pair by the sha256 above. This module only CONSUMES one. It
carries ``source_revision`` (full 40 hex), ``declared_transformations`` (exactly
``DECLARED_TRANSFORMATIONS``), ``sample`` (``restaurant_ids`` non-empty, ``order_ids``),
``tables`` (non-empty: name -> {rows, sha256}), ``media_listing`` (relative path ->
{size, sha256}) and ``golden_reads`` (non-empty: key -> {status, body}) whose keys are
only of ``IDENTITY_READ_KINDS``, plus ``BEARER_READ_KEY`` (the one read an original bearer
token authorises). That read runs only while the token is unexpired, so a golden record of
it can be compared only when the token is unexpired at BOTH ends; otherwise ``R.reads``
is INCOMPLETE, never PASS.

RESULT — schema ``dinify.restore-usability.result/1``. THERE IS NO GLOBAL PASS.
==============================================================================
Five categories, never merged:

* ``P.`` preconditions — any unsatisfied one gives ``run.status = REFUSED`` (exit 3);
* ``I.`` intrinsic — the restored data and the code agree with each other -> ``usable``;
* ``H.`` independent originals -> ``independent``;
* ``R.`` manifest identity -> ``identity``;
* ``O.`` observations — reported beside the rest and NEVER deciding anything.

A category is PASS only when every check in it is PASS. ``usable`` folds UNAVAILABLE
and INCOMPLETE to INCOMPLETE; ``independent`` and ``identity`` are UNAVAILABLE when
nothing was supplied and INCOMPLETE when something was. A check with nothing to check is
INCOMPLETE, never PASS. Exit 0 only when every verdict named in ``require`` is PASS; 1
otherwise; 2 bad command line; 3 REFUSED; 4 INTERRUPTED (timed out or cancelled — every
verdict reads INTERRUPTED and nothing partial is reported as PASS); 70 CRASHED.

WHAT THE READS PROVE, AND WHAT THEY DO NOT
==========================================
* Diner order reads carry a table session signed IN THIS PROCESS from the restored
  table's current generation. That proves a signature ROUND TRIP and authorises the
  read; it proves nothing about any printed sticker. Only ``H.printed_qr``, which scans
  the ORIGINAL credentials and compares the table and restaurant they resolve to, does.
* ``I.public_menu`` holds a served menu to EXACTLY the sections and items the canonical
  publication policy (``menu_publication``) makes visible, evaluated at both ends of the
  read; content whose visibility changed inside the read may be present or absent, and
  nothing else is tolerated. A restaurant with nothing visible compares nothing.
* ``I.order_reads`` holds the published lines to the SAVED lines by identity, quantity
  and saved name: ``items`` lists every row with all its children, ``quote`` the live
  dishes with their live children (``group_live_children``, the endpoint's own split).
* ``I.staff_reads`` holds each list read to page 1 at the application's default size:
  exactly the first ``min(total, 25)`` stored rows in the endpoint's order, with metadata
  stating that page of that population. A full first page passes; no further page is read.
* ``I.kitchen_state`` holds each kitchen feed to the stored orders its endpoint selects:
  every ticket is an order that feed serves, once, agreeing with the stored row, and every
  order it must serve is there. The completed feed's window is evaluated at both ends of
  the read. A correct per-order state read never stands in for a feed, and feeds with
  nothing to serve compare nothing.
* Staff reads are authorised in process (``force_authenticate``) for ONE named, existing,
  active, established restaurant user; the refusals the customer JWT path applies are
  restated, and JWT verification is NOT exercised. Only an ORIGINAL, unexpired bearer
  token crosses the real authentication path (``H.bearer_read``). An expired one is
  evidence about the key and authorises nothing: UNAVAILABLE, never damage.
* ``P.db_readonly`` (a role that cannot write) is PREVENTION. Equal sequence states and
  an unmoved transaction horizon (``O.database_unchanged``) are an OBSERVATION and never
  stand in for it.
* ``I.media_references`` reads through the storage interface and Django's static view —
  local evidence only, never the deployed web server's ``/media/`` alias.

Only ``DECLARED_TRANSFORMATIONS`` are removed before a golden read is compared, exactly
and nothing else; the result lists every path actually removed.
"""
import argparse
import hashlib
import io
import json
import os
import re
import secrets
import signal
import socket
import subprocess
from stat import S_ISREG
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The module scope above imports ONLY the standard library, and must stay that way: this
# file is executed as ``python -m`` BEFORE anything else is imported. Every Django or
# application import below is local to a function that runs after the pre-start phase.

CONTRACT = 'dinify.restore-usability.result/1'
INPUTS_SCHEMA = 'dinify.restore-usability.inputs/1'
MANIFEST_SCHEMA = 'dinify.restore-usability.manifest/1'
BOOTSTRAP_MODULE = 'misc_app.restore_usability'
COMMAND = 'check_restore_usability'

EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_INTERRUPTED, EXIT_CRASHED = 0, 1, 2, 3, 4, 70

STATUSES = ('PASS', 'FAIL', 'UNAVAILABLE', 'INCOMPLETE', 'ATTESTED', 'OBSERVED')
#: The only preconditions that are satisfied by an operator's statement rather than an
#: observation. Every other ``P.`` check must be PASS.
ATTESTED_PRECONDITIONS = frozenset({'P.attestations'})
ATTESTATIONS = ('no_live_routing', 'no_background_work', 'configuration_custody')
VERDICTS = ('usable', 'independent', 'identity')

#: The reads whose bodies are a function of STORED state alone (once the declared paths
#: are removed), and therefore the only ones a backup-time golden read is compared with.
#: The public menu (section schedules evaluated against the clock), the kitchen feeds (a
#: rolling completed window), the staff menu reads (live discount flags) and readiness
#: are clock-dependent: their intrinsic checks still run, but comparing them across time
#: would report a correct restore as different.
IDENTITY_READ_KINDS = ('order.details', 'order.details_by_intent', 'kitchen.state', 'qr.scan')
#: The restaurant list read with an ORIGINAL bearer token. Also compared with a golden read
#: when one was recorded; it is executed only while that token is unexpired.
BEARER_READ_KEY = 'bearer.restaurants'

#: EXACTLY these, and nothing else, are removed before a golden read is compared.
DECLARED_TRANSFORMATIONS = [
    {'reads': 'qr.scan.', 'path': ['data', 'session_token'],
     'why': 'a fresh signature on every scan; not stored state'},
    {'reads': '', 'path': ['data', 'quote_policy', 'status'],
     'why': 'derived from the clock at read time'},
]

DEFAULT_LIMITS = {'max_restaurants': 10, 'max_orders_per_restaurant': 20,
                  'max_sections_per_restaurant': 50, 'max_media_objects': 500}

#: Fixed bounds on reading the two documents, in force BEFORE either is opened. The
#: operator's ``wall_clock_seconds`` is inside the inputs, so it cannot bound this phase.
INPUTS_PHASE_SECONDS = 60
MAX_INPUTS_BYTES = 1 << 20
MAX_MANIFEST_BYTES = 64 << 20

IMAGE_FIELDS = (('restaurants', 'logo'), ('restaurants', 'cover_photo'),
                ('menu_sections', 'section_banner_image'), ('menu_items', 'image'))

INERT_EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
#: Not a URI scheme the Mongo driver accepts: its constructor raises before it opens a
#: socket or starts a monitor thread, and the application's lazy client turns that into
#: "MongoDB is not available" on every use.
INERT_MONGO_URI = 'restore-usability-inert://'
MONGO_PROBE_COLLECTION = '__restore_usability_probe__'

# \Z, never $: '$' also matches before a trailing newline, which would let one through.
HEX40 = re.compile(r'^[0-9a-f]{40}\Z')
HEX64 = re.compile(r'^[0-9a-f]{64}\Z')
NONCE = re.compile(r'^[0-9a-f]{16,64}\Z')
MODULE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z')
DIGITS = re.compile(r'^[0-9]{1,5}\Z')
IFF_UP = 0x1
SIOCGIFFLAGS = 0x8913


# ===================================================================== pure contract

def check(status, **detail):
    if status not in STATUSES:
        raise ValueError(f'unknown check status {status!r}')
    return dict(status=status, **detail)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), default=str).encode()


def preconditions_satisfied(checks):
    for name, c in checks.items():
        if not name.startswith('P.'):
            continue
        allowed = ('PASS', 'ATTESTED') if name in ATTESTED_PRECONDITIONS else ('PASS',)
        if c.get('status') not in allowed:
            return False
    return True


def _fold(group):
    states = [c['status'] for c in group.values()]
    if not states:
        return 'UNAVAILABLE'
    if 'FAIL' in states:
        return 'FAIL'
    if all(s == 'PASS' for s in states):
        return 'PASS'
    if all(s == 'UNAVAILABLE' for s in states):
        return 'UNAVAILABLE'
    return 'INCOMPLETE'


def verdicts(checks, run_status='COMPLETED'):
    """The three verdicts. Never computed from the checks of a run that did not finish."""
    if run_status == 'REFUSED':
        return {v: 'NOT_RUN' for v in VERDICTS}
    if run_status != 'COMPLETED':
        return {v: 'INTERRUPTED' for v in VERDICTS}
    if not preconditions_satisfied(checks):
        return {v: 'NOT_RUN' for v in VERDICTS}

    def cat(prefix):
        return {k: v for k, v in checks.items() if k.startswith(prefix)}

    usable = _fold(cat('I.'))
    if usable in ('UNAVAILABLE', 'INCOMPLETE'):
        usable = 'INCOMPLETE'
    return {'usable': usable, 'independent': _fold(cat('H.')), 'identity': _fold(cat('R.'))}


def exit_code(run_status, verdict, require):
    if run_status == 'REFUSED':
        return EXIT_REFUSED
    if run_status in ('TIMED_OUT', 'CANCELLED'):
        return EXIT_INTERRUPTED
    if run_status != 'COMPLETED':
        return EXIT_CRASHED
    return EXIT_OK if require and all(verdict.get(r) == 'PASS' for r in require) else EXIT_NOT_OK


def apply_transformations(key, body, applied):
    """Remove ONLY the declared paths, from a copy. Records each path actually removed."""
    body = json.loads(json.dumps(body))
    for t in DECLARED_TRANSFORMATIONS:
        if not key.startswith(t['reads']):
            continue
        node = body
        for part in t['path'][:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and t['path'][-1] in node:
            del node[t['path'][-1]]
            applied.add(f"{key}:{'.'.join(t['path'])}")
    return body


def identity_read_key(key):
    if key == BEARER_READ_KEY:
        return True
    kind, _, rest = key.rpartition('.')
    return kind in IDENTITY_READ_KINDS and _is_uuid(rest)


def compare_golden(golden, executed):
    """``R.reads``: the comparison set is EXACTLY the identity reads this run executed.

    PASS needs a non-empty comparison in which every executed identity read has a golden
    record, every golden record was executed, and every pair is byte-equal after the
    declared transformations. Anything less is INCOMPLETE, a difference is FAIL, and an
    empty side is never a PASS by default.
    """
    gold_keys, run_keys = set(golden), set(executed)
    compared = sorted(gold_keys & run_keys)
    differing = [k for k in compared if canon(golden[k]) != canon(executed[k])]
    not_recorded = sorted(run_keys - gold_keys)
    not_executed = sorted(gold_keys - run_keys)
    detail = dict(compared=len(compared), differing=differing,
                  executed_without_golden=not_recorded, golden_not_executed=not_executed)
    if differing:
        return check('FAIL', **detail)
    if not compared:
        return check('INCOMPLETE', reason='nothing was compared', **detail)
    if not_recorded or not_executed:
        return check('INCOMPLETE', reason='the comparison set is not complete', **detail)
    return check('PASS', **detail)


# ===================================================================== input validation

def _is_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


class _Problems(list):
    def add(self, path, message):
        self.append(f'{path}: {message}')


def _object(value, path, problems, required=(), optional=()):
    if not isinstance(value, dict):
        problems.add(path, 'must be an object')
        return None
    for key in sorted(set(value) - set(required) - set(optional)):
        problems.add(f'{path}.{key}', 'unknown key')
    for key in required:
        if key not in value:
            problems.add(f'{path}.{key}', 'required')
    return value


def _string(value, path, problems, pattern=None, max_len=4096):
    if not isinstance(value, str) or not value.strip():
        problems.add(path, 'must be a non-empty string')
        return None
    if len(value) > max_len:
        problems.add(path, f'longer than {max_len} characters')
        return None
    if pattern is not None and not pattern.match(value):
        problems.add(path, 'has the wrong format')
        return None
    return value


def _uuid(value, path, problems):
    if not _is_uuid(value):
        problems.add(path, 'must be a canonical lowercase UUID string')
        return None
    return value


def _int(value, path, problems, minimum=1):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        problems.add(path, f'must be an integer >= {minimum}')
        return None
    return value


def _abspath(value, path, problems):
    if _string(value, path, problems) is None:
        return None
    if not os.path.isabs(value):
        problems.add(path, 'must be an absolute path')
        return None
    return value


def _records(value, path, problems):
    if not isinstance(value, list) or not value:
        problems.add(path, 'must be a non-empty list (omit the key when there is nothing to supply)')
        return []
    return value


def validate_inputs(doc):
    """Return (normalised inputs, problems). Every problem is reported, not the first."""
    p = _Problems()
    doc = _object(doc, 'inputs', p, required=('schema', 'release', 'target', 'limits', 'attestations'),
                  optional=('sample', 'staff_principal', 'originals', 'manifest', 'require'))
    if doc is None:
        return None, list(p)
    if doc.get('schema') != INPUTS_SCHEMA:
        p.add('inputs.schema', f'must be {INPUTS_SCHEMA!r}')

    rel = _object(doc.get('release'), 'inputs.release', p, required=('source_revision',))
    if rel is not None and 'source_revision' in rel:
        _string(rel['source_revision'], 'inputs.release.source_revision', p, pattern=HEX40)

    tgt = _object(doc.get('target'), 'inputs.target', p,
                  required=('settings_module', 'database', 'media_root'))
    if tgt is not None:
        if 'settings_module' in tgt:
            _string(tgt['settings_module'], 'inputs.target.settings_module', p, pattern=MODULE_NAME, max_len=200)
        db = _object(tgt.get('database'), 'inputs.target.database', p, required=('name', 'user', 'host', 'port'))
        if db is not None:
            for k in ('name', 'user'):
                if k in db:
                    _string(db[k], f'inputs.target.database.{k}', p, max_len=200)
            if 'host' in db:
                _abspath(db['host'], 'inputs.target.database.host', p)
            if 'port' in db:
                _string(db['port'], 'inputs.target.database.port', p, pattern=DIGITS)
        if 'media_root' in tgt:
            _abspath(tgt['media_root'], 'inputs.target.media_root', p)

    limits = dict(DEFAULT_LIMITS)
    lim = _object(doc.get('limits'), 'inputs.limits', p, required=('wall_clock_seconds',),
                  optional=tuple(DEFAULT_LIMITS))
    if lim is not None:
        for k, v in lim.items():
            if k in limits or k == 'wall_clock_seconds':
                if _int(v, f'inputs.limits.{k}', p) is not None:
                    limits[k] = v

    att = _object(doc.get('attestations'), 'inputs.attestations', p,
                  required=ATTESTATIONS + ('attested_by',))
    if att is not None:
        for k in ATTESTATIONS:
            if k in att and att[k] is not True:
                p.add(f'inputs.attestations.{k}', 'must be exactly true (the run is refused without it)')
        if 'attested_by' in att:
            _string(att['attested_by'], 'inputs.attestations.attested_by', p, max_len=200)

    if 'sample' in doc:
        s = _object(doc['sample'], 'inputs.sample', p, required=('restaurant_ids',))
        if s is not None and 'restaurant_ids' in s:
            ids = _records(s['restaurant_ids'], 'inputs.sample.restaurant_ids', p)
            for i, rid in enumerate(ids):
                _uuid(rid, f'inputs.sample.restaurant_ids[{i}]', p)
            if len(set(map(str, ids))) != len(ids):
                p.add('inputs.sample.restaurant_ids', 'contains duplicates')
            if isinstance(limits.get('max_restaurants'), int) and len(ids) > limits['max_restaurants']:
                p.add('inputs.sample.restaurant_ids', 'is longer than limits.max_restaurants')
        if 'manifest' in doc:
            p.add('inputs.sample', 'must be omitted when a manifest is supplied (the manifest names its own sample)')

    if 'staff_principal' in doc:
        sp = _object(doc['staff_principal'], 'inputs.staff_principal', p, required=('user_id',))
        if sp is not None and 'user_id' in sp:
            _uuid(sp['user_id'], 'inputs.staff_principal.user_id', p)

    if 'originals' in doc:
        _validate_originals(doc['originals'], p)

    if 'manifest' in doc:
        m = _object(doc['manifest'], 'inputs.manifest', p, required=('path', 'sha256'))
        if m is not None:
            if 'path' in m:
                _abspath(m['path'], 'inputs.manifest.path', p)
            if 'sha256' in m:
                _string(m['sha256'], 'inputs.manifest.sha256', p, pattern=HEX64)

    require = doc.get('require', ['usable'])
    if (not isinstance(require, list) or not require
            or any(not isinstance(r, str) or r not in VERDICTS for r in require)
            or len(set(require)) != len(require)):
        p.add('inputs.require', f'must be a non-empty list of distinct names from {list(VERDICTS)}')

    if p:
        return None, list(p)
    out = dict(doc)
    out['limits'] = limits
    out['require'] = list(require)
    return out, []


def _validate_originals(orig, p):
    known = ('printed_qr', 'orders', 'staff_bearer_token')
    orig = _object(orig, 'inputs.originals', p, optional=known)
    if orig is None:
        return
    if not any(k in orig for k in known):
        p.add('inputs.originals', 'is empty (omit it when no originals are supplied)')
    if 'printed_qr' in orig:
        seen = set()
        for i, rec in enumerate(_records(orig['printed_qr'], 'inputs.originals.printed_qr', p)):
            path = f'inputs.originals.printed_qr[{i}]'
            rec = _object(rec, path, p, required=('table_id', 'restaurant_id', 'credential'))
            if rec is None:
                continue
            tid = _uuid(rec.get('table_id'), f'{path}.table_id', p) if 'table_id' in rec else None
            if 'restaurant_id' in rec:
                _uuid(rec['restaurant_id'], f'{path}.restaurant_id', p)
            if 'credential' in rec:
                _string(rec['credential'], f'{path}.credential', p)
            if tid is not None:
                if tid in seen:
                    p.add(f'{path}.table_id', 'duplicates an earlier record')
                seen.add(tid)
    if 'orders' in orig:
        seen = set()
        for i, rec in enumerate(_records(orig['orders'], 'inputs.originals.orders', p)):
            path = f'inputs.originals.orders[{i}]'
            rec = _object(rec, path, p, required=('order_id', 'restaurant_id', 'table_id', 'state'),
                          optional=('quote_ref',))
            if rec is None:
                continue
            oid = _uuid(rec.get('order_id'), f'{path}.order_id', p) if 'order_id' in rec else None
            for k in ('restaurant_id', 'table_id'):
                if k in rec:
                    _uuid(rec[k], f'{path}.{k}', p)
            state = rec.get('state')
            if 'state' in rec and state not in ('accepted', 'exists'):
                p.add(f'{path}.state', "must be 'accepted' or 'exists'")
            if state == 'accepted':
                if 'quote_ref' not in rec:
                    p.add(f'{path}.quote_ref', "required when state is 'accepted'")
                else:
                    _string(rec['quote_ref'], f'{path}.quote_ref', p, max_len=200)
            elif state == 'exists' and 'quote_ref' in rec:
                p.add(f'{path}.quote_ref', "not compared for state 'exists'; omit it")
            if oid is not None:
                if oid in seen:
                    p.add(f'{path}.order_id', 'duplicates an earlier record')
                seen.add(oid)
    if 'staff_bearer_token' in orig:
        _string(orig['staff_bearer_token'], 'inputs.originals.staff_bearer_token', p)


def validate_manifest(doc, limits):
    p = _Problems()
    doc = _object(doc, 'manifest', p, required=('schema', 'source_revision', 'declared_transformations',
                                                'sample', 'tables', 'media_listing', 'golden_reads'))
    if doc is None:
        return list(p)
    if doc.get('schema') != MANIFEST_SCHEMA:
        p.add('manifest.schema', f'must be {MANIFEST_SCHEMA!r}')
    if 'source_revision' in doc:
        _string(doc['source_revision'], 'manifest.source_revision', p, pattern=HEX40)
    if 'declared_transformations' in doc and doc['declared_transformations'] != DECLARED_TRANSFORMATIONS:
        p.add('manifest.declared_transformations', 'differ from this build; refusing to compare')
    s = _object(doc.get('sample'), 'manifest.sample', p, required=('restaurant_ids', 'order_ids'))
    if s is not None:
        rids = _records(s.get('restaurant_ids'), 'manifest.sample.restaurant_ids', p) if 'restaurant_ids' in s else []
        for i, v in enumerate(rids):
            _uuid(v, f'manifest.sample.restaurant_ids[{i}]', p)
        if len(rids) > limits.get('max_restaurants', 0):
            p.add('manifest.sample.restaurant_ids', 'is longer than limits.max_restaurants')
        oids = s.get('order_ids')
        if 'order_ids' in s and not isinstance(oids, list):
            p.add('manifest.sample.order_ids', 'must be a list')
        for i, v in enumerate(oids if isinstance(oids, list) else []):
            _uuid(v, f'manifest.sample.order_ids[{i}]', p)
        if isinstance(oids, list) and rids and len(oids) > limits.get('max_orders_per_restaurant', 0) * len(rids):
            p.add('manifest.sample.order_ids', 'exceeds the order limits for the sample')
    tables = doc.get('tables')
    if 'tables' in doc:
        if not isinstance(tables, dict) or not tables:
            p.add('manifest.tables', 'must be a non-empty object')
        else:
            for name, rec in tables.items():
                r = _object(rec, f'manifest.tables.{name}', p, required=('rows', 'sha256'))
                if r is not None:
                    _int(r.get('rows'), f'manifest.tables.{name}.rows', p, minimum=0)
                    _string(r.get('sha256'), f'manifest.tables.{name}.sha256', p, pattern=HEX64)
    media = doc.get('media_listing')
    if 'media_listing' in doc:
        if not isinstance(media, dict):
            p.add('manifest.media_listing', 'must be an object')
        else:
            for name, rec in media.items():
                r = _object(rec, f'manifest.media_listing.{name}', p, required=('size', 'sha256'))
                if r is not None:
                    _int(r.get('size'), f'manifest.media_listing.{name}.size', p, minimum=0)
                    _string(r.get('sha256'), f'manifest.media_listing.{name}.sha256', p, pattern=HEX64)
    gold = doc.get('golden_reads')
    if 'golden_reads' in doc:
        if not isinstance(gold, dict) or not gold:
            p.add('manifest.golden_reads', 'must be a non-empty object (an empty set compares nothing)')
        else:
            for key, rec in gold.items():
                path = f'manifest.golden_reads.{key}'
                if not identity_read_key(key):
                    p.add(path, f'is not an identity read ({list(IDENTITY_READ_KINDS) + [BEARER_READ_KEY]})')
                r = _object(rec, path, p, required=('status', 'body'))
                if r is not None and 'status' in r:
                    _int(r['status'], f'{path}.status', p, minimum=100)
    return list(p)


def redacted_inputs(inputs):
    """What the result may say about the inputs: no credential, ever."""
    red = json.loads(json.dumps(inputs))
    orig = red.get('originals')
    if isinstance(orig, dict):
        if 'printed_qr' in orig:
            orig['printed_qr'] = [{'table_id': r.get('table_id'), 'restaurant_id': r.get('restaurant_id')}
                                  for r in orig['printed_qr']]
        if 'staff_bearer_token' in orig:
            orig['staff_bearer_token'] = 'supplied (redacted)'
    return red


def redacted_digest(inputs):
    return sha256(canon(redacted_inputs(inputs)))


# ===================================================================== pre-start observers (stdlib)

def checkout_root():
    return Path(__file__).resolve().parent.parent


def _local_packages(root):
    try:
        return sorted(d.name for d in root.iterdir() if (d / '__init__.py').is_file())
    except OSError:
        return []


def observe_entry(settings_module, modules=None, main_module=None, environ=None):
    """``P.entrypoint``: the supported bootstrap, entered before anything it must precede."""
    modules = sys.modules if modules is None else modules
    main_module = sys.modules.get('__main__') if main_module is None else main_module
    environ = os.environ if environ is None else environ
    problems = []
    spec = getattr(main_module, '__spec__', None)
    if getattr(spec, 'name', None) != BOOTSTRAP_MODULE:
        problems.append(f'not entered as `python -m {BOOTSTRAP_MODULE}`')
    roots = {'django', 'rest_framework', 'rest_framework_simplejwt', 'corsheaders', 'psycopg', 'pymongo'}
    roots.update(_local_packages(checkout_root()))
    if isinstance(settings_module, str) and settings_module:
        roots.add(settings_module.split('.')[0])
    allowed = {'misc_app', BOOTSTRAP_MODULE}
    early = sorted(m for m in modules if m.split('.')[0] in roots and m not in allowed)
    if early:
        problems.append(f'{len(early)} framework/application module(s) were already imported')
    configured = environ.get('DJANGO_SETTINGS_MODULE')
    if configured and configured != settings_module:
        problems.append('DJANGO_SETTINGS_MODULE in the environment differs from inputs.target.settings_module')
    return check('FAIL' if problems else 'PASS', problems=problems, imported_too_early=early[:20])


def _run_git(root, *args):
    return subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def observe_source_identity(expected_revision, root=None):
    """``P.source_identity``: a clean git checkout at exactly the pinned full revision."""
    root = checkout_root() if root is None else root
    try:
        top = _run_git(root, 'rev-parse', '--show-toplevel')
        head = _run_git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
        status = _run_git(root, 'status', '--porcelain=v1', '--untracked-files=all')
    except (OSError, subprocess.SubprocessError) as exc:
        return check('FAIL', reason=f'git could not be run ({type(exc).__name__})')
    problems = []
    if top.returncode or Path(top.stdout.strip()).resolve() != root:
        problems.append('the module is not loaded from the root of a git checkout')
    revision = head.stdout.strip()
    if head.returncode or not HEX40.match(revision):
        problems.append('HEAD is unreadable')
    elif revision != expected_revision:
        problems.append('HEAD is not inputs.release.source_revision')
    if status.returncode:
        problems.append('the working tree state is unreadable')
    elif status.stdout.strip():
        problems.append(f'the working tree is not clean ({len(status.stdout.splitlines())} path(s))')
    return check('FAIL' if problems else 'PASS', revision=revision if HEX40.match(revision) else None,
                 checkout=str(root), problems=problems)


def _normal(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def observe_runtime(root=None):
    """``P.runtime``: this interpreter and its installed distributions ARE the lock's."""
    import importlib.metadata
    import platform
    root = checkout_root() if root is None else root
    try:
        lock = json.loads((root / 'release' / 'python-lock.json').read_text())
        target = lock['target']
        expected = {_normal(e['name']): e['version'] for e in lock['packages'] + lock['bootstrap']}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return check('FAIL', reason=f'release/python-lock.json is unreadable ({type(exc).__name__})')
    problems = []
    libc = platform.libc_ver()
    facts = {'python': platform.python_version(), 'implementation': platform.python_implementation(),
             'platform': sys.platform, 'machine': platform.machine(),
             'glibc': libc[1] if libc[0] == 'glibc' else None}
    for k, v in facts.items():
        if target.get(k) != v:
            problems.append(f'{k} is {v!r}, the lock targets {target.get(k)!r}')
    installed, duplicated = {}, set()
    for dist in importlib.metadata.distributions():
        name = _normal(dist.metadata['Name'] or '')
        if name in installed:
            duplicated.add(name)
        installed[name] = dist.version
    missing = sorted(set(expected) - set(installed))
    extra = sorted(set(installed) - set(expected))
    wrong = sorted(n for n in set(expected) & set(installed) if expected[n] != installed[n])
    if missing or extra or wrong or duplicated:
        problems.append('the installed distributions are not exactly the lock')
    return check('FAIL' if problems else 'PASS', problems=problems, runtime=facts, distributions=len(installed),
                 missing=missing, unexpected=extra, wrong_version=wrong, duplicated=sorted(duplicated))


def _interface_flags(name):
    """Flags from THIS process's network namespace (``SIOCGIFFLAGS``). Deliberately not
    /sys/class/net, which reflects the namespace that mounted sysfs. Creating an
    unconnected socket is not an outbound attempt."""
    import fcntl
    import struct
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, struct.pack('16sH14x', name.encode()[:15], 0))
        return struct.unpack('16sH14x', r)[1]
    finally:
        s.close()


def _route_lines(path):
    with open(path) as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip()]


def observe_outbound_denied(interfaces=None, flags=None, ipv4_routes=None, ipv6_routes=None):
    """``P.outbound_denied``: no interface is up and no route exists, in THIS namespace.

    A kernel fact, not a Python guard: with no interface up and no route, no provider —
    SMTP, SMS gateway, Mongo, DNS — can be reached by this process or its children. A
    database is still reachable through a Unix socket, which is why the target requires
    one. Anything that cannot be read FAILS the check rather than being skipped.
    """
    up, unreadable = [], []
    try:
        names = [n for _i, n in (socket.if_nameindex() if interfaces is None else interfaces)]
        for name in names:
            try:
                value = (flags or _interface_flags)(name)
            except OSError as exc:
                unreadable.append(f'{name}:{type(exc).__name__}')
                continue
            if value & IFF_UP:
                up.append(name)
        v4 = _route_lines('/proc/net/route')[1:] if ipv4_routes is None else ipv4_routes
        v6 = (_route_lines('/proc/net/ipv6_route') if os.path.exists('/proc/net/ipv6_route') else []) \
            if ipv6_routes is None else ipv6_routes
    except (OSError, AttributeError) as exc:
        return check('FAIL', reason=f'the network namespace could not be inspected ({type(exc).__name__})')
    ok = not up and not unreadable and not v4 and not v6
    return check('PASS' if ok else 'FAIL', interfaces_up=up, interfaces_unreadable=unreadable,
                 ipv4_routes=len(v4), ipv6_routes=len(v6))


def observe_media_readonly(root):
    """``P.media_readonly``: the media root is a read-only mount (not merely unwritable)."""
    if not isinstance(root, str) or not os.path.isdir(root):
        return check('FAIL', reason='the media root does not exist')
    try:
        read_only_mount = bool(os.statvfs(root).f_flag & os.ST_RDONLY)
    except OSError as exc:
        return check('FAIL', reason=f'the media mount could not be inspected ({type(exc).__name__})')
    writable = os.access(root, os.W_OK)
    return check('PASS' if read_only_mount and not writable else 'FAIL',
                 read_only_mount=read_only_mount, writable=writable)


def observe_attestations(att):
    if not isinstance(att, dict) or any(att.get(k) is not True for k in ATTESTATIONS):
        return check('FAIL', reason='every operational precondition must be attested true')
    return check('ATTESTED', attested=list(ATTESTATIONS), attested_by=att.get('attested_by'),
                 note='stated by the operator; NOT observed by this process')


# ===================================================================== bounded execution

class Interrupted(KeyboardInterrupt):
    """Raised by the deadline or a cancellation signal.

    A ``KeyboardInterrupt`` subclass on purpose: ordinary application code catches
    ``Exception`` and must not swallow it, and the PostgreSQL driver answers a
    ``KeyboardInterrupt`` raised during a query by cancelling that query on the server
    before re-raising, so an interrupted read does not keep running there.
    """
    status = 'CANCELLED'


class DeadlineExceeded(Interrupted):
    status = 'TIMED_OUT'


class Cancelled(Interrupted):
    status = 'CANCELLED'


def _on_alarm(_signum, _frame):
    raise DeadlineExceeded()


def _on_cancel(_signum, _frame):
    raise Cancelled()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _children(pid):
    out = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat.rsplit(')', 1)[-1].split()
        if len(fields) > 1 and fields[1] == str(pid):
            out.append(int(entry))
    return out


def close_resources():
    """Close what this run opened, and report what is left. Called on EVERY ending."""
    report = {}
    if 'django.db' in sys.modules:
        from django.db import connections
        try:
            connections.close_all()
            report['database_connections'] = 'closed'
        except Exception as exc:          # recorded, never raised from the cleanup path
            report['database_connections'] = f'close failed ({type(exc).__name__})'
    children = _children(os.getpid())
    if children is None:
        report['child_processes'] = 'unknown'
    else:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        report['child_processes_reaped'] = len(children)
    import threading
    report['other_threads'] = sorted(t.name for t in threading.enumerate()
                                     if t is not threading.main_thread() and t.is_alive())
    return report


def supervise(result, deadline_seconds, body):
    """Run ``body`` under a wall-clock bound and a cancellation handler, then FINALISE.

    The run ends in exactly one of COMPLETED, REFUSED, TIMED_OUT, CANCELLED or CRASHED.
    Only COMPLETED computes verdicts from the checks; every other ending reports what was
    reached and marks every verdict NOT_RUN or INTERRUPTED. Resources are closed on every
    ending, and the timer and handlers are restored before the result is written. A signal
    that arrives once the work has ended is recorded in ``run.late_signals`` and changes
    nothing; once the previous handlers are back, a later one acts as it would have
    without this module (a SIGTERM then ends the process before any result is published,
    which is never reported as a PASS).
    """
    previous = {}
    for sig, handler in ((signal.SIGTERM, _on_cancel), (signal.SIGINT, _on_cancel),
                         (signal.SIGALRM, _on_alarm)):
        previous[sig] = signal.signal(sig, handler)
    run, late = result['run'], []

    def record_late(signum, _frame):
        late.append(signal.Signals(signum).name)

    try:
        try:
            if deadline_seconds:
                signal.setitimer(signal.ITIMER_REAL, float(deadline_seconds))
            body()
            run['status'] = 'REFUSED' if run.get('refused_before') else 'COMPLETED'
        finally:
            # From here on a signal is RECORDED, never raised, so nothing can escape the
            # classification and cleanup below. One that lands before these lines finish
            # is still raised inside this try and classified like any other.
            for sig in previous:
                signal.signal(sig, record_late)
            signal.setitimer(signal.ITIMER_REAL, 0)
    except Interrupted as exc:
        run['status'] = exc.status
    except KeyboardInterrupt:
        run['status'] = 'CANCELLED'
    except Exception as exc:
        run['status'] = 'CRASHED'
        run['crash'] = {'type': f'{type(exc).__module__}.{type(exc).__name__}', 'phase': run.get('phase')}
    finally:
        result['resources'] = close_resources()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if late:
            run['late_signals'] = late      # arrived after the work had ended; reported, not acted on
    finalise(result)
    return result


def finalise(result):
    run = result['run']
    result['verdicts'] = verdicts(result['checks'], run['status'])
    result['exit'] = exit_code(run['status'], result['verdicts'], result.get('require') or [])
    result['finished_at'] = _now()
    if run['status'] != 'COMPLETED':
        result['partial'] = run['status'] != 'REFUSED'
    return result


def publish(path, result):
    """Write the result atomically, and NEVER over an existing file."""
    data = (json.dumps(result, indent=1, sort_keys=True, default=str) + '\n').encode()
    tmp = f'{path}.partial-{result.get("nonce")}-{os.getpid()}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


def summary(result, path):
    return {'nonce': result.get('nonce'), 'run': result['run'].get('status'),
            'refused_before': result['run'].get('refused_before'),
            'verdicts': result.get('verdicts'), 'require': result.get('require'), 'exit': result.get('exit'),
            'failed': sorted(k for k, c in result['checks'].items() if c.get('status') == 'FAIL'),
            'not_pass': sorted(k for k, c in result['checks'].items()
                               if c.get('status') in ('UNAVAILABLE', 'INCOMPLETE')),
            'result': str(path)}


# ===================================================================== bootstrap → adapter handover

class BootstrapProof:
    """The in-process handover from the bootstrap to the management adapter.

    Not a security boundary against code running in the same interpreter: it exists so
    that an ordinary ``manage.py`` invocation — which has already loaded the settings and
    initialised Django — can never be mistaken for the bootstrapped path. It is issued
    only after the pre-start phase, registered here, and accepted once.
    """
    __slots__ = ('token', 'result', 'inputs', 'manifest', 'deadline_at', 'consumed')

    def __init__(self, token, result, inputs, manifest, deadline_at):
        self.token, self.result, self.inputs = token, result, inputs
        self.manifest, self.deadline_at, self.consumed = manifest, deadline_at, False


_ISSUED = {}


class BootstrapRefused(Exception):
    pass


def issue_proof(result, inputs, manifest, deadline_at):
    proof = BootstrapProof(secrets.token_hex(16), result, inputs, manifest, deadline_at)
    _ISSUED[proof.token] = proof
    return proof


def accept_proof(candidate):
    if not isinstance(candidate, BootstrapProof):
        raise BootstrapRefused('no bootstrap handover was supplied')
    if _ISSUED.get(candidate.token) is not candidate:
        raise BootstrapRefused('the handover was not issued by this process\'s bootstrap')
    if candidate.consumed:
        raise BootstrapRefused('the handover has already been used')
    candidate.consumed = True
    return candidate


def new_result(nonce):
    return {'contract': CONTRACT, 'nonce': nonce, 'started_at': _now(), 'pid': os.getpid(),
            'run': {'status': None, 'phase': 'start'}, 'checks': {}, 'observations': {},
            'require': ['usable'], 'declared_transformations': DECLARED_TRANSFORMATIONS,
            'scope': 'customer/application reads of a restored backend; not an Admin recovery suite'}


def _parser():
    p = argparse.ArgumentParser(
        prog=f'python -m {BOOTSTRAP_MODULE}',
        description='D15 R3 restore-usability check: bounded, read-only checks of a RESTORED backend, '
                    'in the supported isolated source-checkout profile. See the module docstring '
                    'for the inputs schema, the order of checks and what the reads prove.',
        epilog=f'Inputs schema {INPUTS_SCHEMA}; result {CONTRACT}. Exit: 0 every required verdict PASS, '
               '1 otherwise, 2 bad command line, 3 REFUSED, 4 INTERRUPTED, 70 CRASHED. '
               'There is no global PASS.')
    p.add_argument('--inputs', required=True, help='the inputs document (JSON)')
    p.add_argument('--result', required=True, help='where to publish the result; must not exist')
    p.add_argument('--nonce', required=True, help='16-64 lowercase hex chars, echoed in the result')
    return p


def main(argv=None):
    """The supported entry point: ``python -m misc_app.restore_usability``."""
    entry_modules = dict(sys.modules)        # what was imported BEFORE this function ran
    args = _parser().parse_args(argv)
    if not NONCE.match(args.nonce):
        print('restore-usability: --nonce must be 16-64 lowercase hex characters', file=sys.stderr)
        return EXIT_USAGE
    result_path = Path(args.result).absolute()
    if result_path.exists() or not result_path.parent.is_dir():
        print('restore-usability: --result must name a new file in an existing directory', file=sys.stderr)
        return EXIT_USAGE

    result = new_result(args.nonce)
    result['bounds'] = {'inputs_phase_seconds': INPUTS_PHASE_SECONDS, 'max_inputs_bytes': MAX_INPUTS_BYTES,
                        'max_manifest_bytes': MAX_MANIFEST_BYTES}
    state = {}
    supervise(result, INPUTS_PHASE_SECONDS, lambda: _load_inputs(args.inputs, result, state))
    if result['run']['status'] == 'COMPLETED':
        result['run']['status'] = None
        inputs = state['inputs']
        deadline_at = time.monotonic() + inputs['limits']['wall_clock_seconds']
        result['bounds']['wall_clock_seconds'] = inputs['limits']['wall_clock_seconds']
        supervise(result, inputs['limits']['wall_clock_seconds'],
                  lambda: _bootstrap(inputs, state.get('manifest'), result, entry_modules, deadline_at))
    publish(result_path, result)
    print(json.dumps(summary(result, result_path), indent=1, sort_keys=True))
    return result['exit']


def read_bounded(path, limit):
    """The bytes of a REGULAR file, opened without blocking and never read past ``limit``.

    Returns ``(data, None)`` or ``(None, problem)``. A FIFO, a device or a directory is
    refused unread — opening a FIFO with no writer would otherwise block before any bound
    applied. Anything larger than ``limit`` is refused too: at most ``limit + 1`` bytes are
    ever read, whatever size the file reports or grows to while it is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    except (OSError, ValueError, TypeError) as exc:
        return None, f'unreadable ({type(exc).__name__})'
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            return None, 'not a regular file'
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 16, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            return None, f'larger than {limit} bytes'
        return b''.join(chunks), None
    except OSError as exc:
        return None, f'unreadable ({type(exc).__name__})'
    finally:
        os.close(fd)


def _load_inputs(path, result, state):
    result['run']['phase'] = 'inputs'
    problems = []
    raw = None
    data, problem = read_bounded(path, MAX_INPUTS_BYTES)
    if problem is not None:
        problems = [f'the inputs document: {problem}']
    else:
        try:
            raw = json.loads(data)
        except (ValueError, RecursionError) as exc:
            problems = [f'the inputs document could not be read as JSON ({type(exc).__name__})']
    inputs = None
    if raw is not None:
        inputs, problems = validate_inputs(raw)
    manifest = None
    if inputs is not None and 'manifest' in inputs:
        data, problem = read_bounded(inputs['manifest']['path'], MAX_MANIFEST_BYTES)
        if problem is not None:
            problems.append(f'manifest: {problem}')
        else:
            if sha256(data) != inputs['manifest']['sha256']:
                problems.append('manifest: its bytes do not match inputs.manifest.sha256')
            else:
                try:
                    manifest = json.loads(data)
                except (ValueError, RecursionError):
                    problems.append('manifest: not JSON')
                else:
                    problems += validate_manifest(manifest, inputs['limits'])
    if inputs is not None:
        result['require'] = inputs['require']
        result['inputs'] = {'redacted_digest': redacted_digest(inputs),
                            'summary': {k: redacted_inputs(inputs).get(k) for k in
                                        ('release', 'target', 'limits', 'attestations', 'sample',
                                         'staff_principal', 'originals', 'manifest')}}
    result['checks']['P.inputs'] = check('FAIL' if problems else 'PASS', problems=problems)
    if problems:
        result['run']['refused_before'] = 'any other check (the inputs are not acceptable)'
        return
    state['inputs'], state['manifest'] = inputs, manifest


def _bootstrap(inputs, manifest, result, entry_modules, deadline_at):
    C, run = result['checks'], result['run']
    tgt = inputs['target']
    run['phase'] = 'pre-start'
    C['P.entrypoint'] = observe_entry(tgt['settings_module'], modules=entry_modules)
    C['P.source_identity'] = observe_source_identity(inputs['release']['source_revision'])
    C['P.runtime'] = observe_runtime()
    C['P.outbound_denied'] = observe_outbound_denied()
    C['P.media_readonly'] = observe_media_readonly(tgt['media_root'])
    C['P.attestations'] = observe_attestations(inputs['attestations'])
    if not preconditions_satisfied(C):
        run['refused_before'] = 'importing the settings or any application code'
        return
    # Configured controls, established before the settings exist.
    os.environ['MONGO_HOST'] = INERT_MONGO_URI
    os.environ['DJANGO_SETTINGS_MODULE'] = tgt['settings_module']
    result['controls'] = {'mongo': 'connection target set to an unparseable URI before the settings loaded',
                          'settings_module': tgt['settings_module']}

    run['phase'] = 'settings'
    from django.conf import settings
    settings.INSTALLED_APPS                                    # import the settings module now
    C.update(check_settings(inputs, settings))
    if not preconditions_satisfied(C):
        run['refused_before'] = 'django.setup()'
        return
    settings.EMAIL_BACKEND = INERT_EMAIL_BACKEND
    result['controls']['email'] = 'in-memory backend set in this process before django.setup()'

    run['phase'] = 'setup'
    import django
    django.setup()
    from django.core.management import call_command
    proof = issue_proof(result, inputs, manifest, deadline_at)
    run['phase'] = 'adapter'
    call_command(COMMAND, bootstrap=proof)


# ===================================================================== settings-phase checks

def http_host(allowed_hosts):
    """A host the restored configuration accepts, for in-process requests (no listener)."""
    for h in allowed_hosts or ():
        if h == '*':
            return 'restore-check.invalid'
        if h.startswith('.') and len(h) > 1:
            return 'restore-check' + h
        if h and '*' not in h:
            return h
    return None


def check_settings(inputs, settings):
    """``P.target_identity`` and ``P.signing_keys_present``: settings read, Django not set up."""
    tgt = inputs['target']
    want = tgt['database']
    db = settings.DATABASES.get('default') or {}
    problems = []
    if db.get('ENGINE') != 'django.db.backends.postgresql':
        problems.append('the default database is not PostgreSQL')
    for key, field in (('name', 'NAME'), ('user', 'USER'), ('host', 'HOST'), ('port', 'PORT')):
        if str(db.get(field) or '') != want[key]:
            problems.append(f'DATABASES.default.{field} differs from inputs.target.database.{key}')
    host = str(db.get('HOST') or '')
    if not (os.path.isabs(host) and os.path.isdir(host)):
        problems.append('the database is not reached through a Unix-socket directory')
    if os.path.realpath(str(settings.MEDIA_ROOT)) != os.path.realpath(tgt['media_root']):
        problems.append('MEDIA_ROOT differs from inputs.target.media_root')
    if http_host(getattr(settings, 'ALLOWED_HOSTS', ())) is None:
        problems.append('ALLOWED_HOSTS has no entry usable for in-process reads')
    keys, key_problems = ('SECRET_KEY', 'DINER_CAP_KEY'), []
    for k in keys:
        if not getattr(settings, k, None):
            key_problems.append(f'{k} is absent')
    if getattr(settings, 'SECRET_KEY', None) and getattr(settings, 'SECRET_KEY', None) == getattr(
            settings, 'DINER_CAP_KEY', None):
        key_problems.append('SECRET_KEY equals DINER_CAP_KEY')
    try:
        from decouple import config
        if config('DINER_CAP_KEY', default=None) in (None, ''):
            key_problems.append('DINER_CAP_KEY is not configured explicitly (a derived development key is refused)')
    except Exception as exc:
        key_problems.append(f'the configuration source could not be read ({type(exc).__name__})')
    return {
        'P.target_identity': check('FAIL' if problems else 'PASS', problems=problems,
                                   database={'name': want['name'], 'user': want['user']}),
        'P.signing_keys_present': check('FAIL' if key_problems else 'PASS', keys_checked=list(keys),
                                        problems=key_problems,
                                        note='presence and distinctness only; no value is read out or emitted'),
    }


# ===================================================================== adapter phase (Django set up)

def run_checks(proof):
    """Everything after ``django.setup()``. Called by the management adapter only."""
    result, inputs, manifest = proof.result, proof.inputs, proof.manifest
    C, O, run = result['checks'], result['observations'], result['run']
    from django.db import connection
    run['phase'] = 'post-setup preconditions'
    C['P.providers_inert'] = check_providers_inert()
    C['P.db_readonly'] = check_db_readonly(connection)
    C['P.db_exclusive'] = check_db_exclusive(connection)
    if not preconditions_satisfied(C):
        run['refused_before'] = 'any application read'
        return
    remaining_ms = max(1, int((proof.deadline_at - time.monotonic()) * 1000))
    with connection.cursor() as cur:
        cur.execute("select set_config('statement_timeout', %s, false)", [str(remaining_ms)])
    t0 = time.monotonic()
    run['phase'] = 'observations (before)'
    before = database_facts(connection)
    run['phase'] = 'sample'
    sample, source = choose_sample(inputs, manifest)
    result['sample'] = {'source': source, 'restaurant_ids': sample['restaurant_ids'],
                        'order_ids': sample['order_ids'],
                        'not_sampled': sample.get('not_sampled', 'decided at backup time (manifest)')}
    principal, principal_note = resolve_principal(inputs)
    run['phase'] = 'reads'
    reads = Reads(principal)
    plan = read_plan(reads, inputs, sample, principal)
    run['phase'] = 'intrinsic checks'
    C.update(intrinsic_checks(connection, reads, plan, inputs, principal_note))
    O.update(order_observations(sample))
    run['phase'] = 'independent checks'
    C.update(independent_checks(inputs, reads, plan))
    run['phase'] = 'identity checks'
    executed, applied = identity_reads(reads)
    C.update(identity_checks(manifest, connection, executed, plan['media_refs'], result))
    result['transformations_applied'] = applied
    run['phase'] = 'observations (after)'
    after = database_facts(connection)
    O['O.database_unchanged'] = check(
        'OBSERVED', sequences_equal=before['sequences'] == after['sequences'],
        xact_horizon_before=before['xact_horizon'], xact_horizon_after=after['xact_horizon'],
        other_clients_after=after['other_clients'],
        note='an observation, never prevention (P.db_readonly prevents). The horizon is cluster-wide and '
             'one past the newest COMPLETED transaction: a moved horizon shows some transaction completed, '
             'but an unchanged one does not prove none did — an older transaction that was already active '
             'can complete without advancing it.')
    from django.core import mail
    O['O.mail_captured'] = check('OBSERVED', messages=len(getattr(mail, 'outbox', []) or []),
                                 note='in-memory backend; nothing was delivered')
    result['authorisation'] = {
        'diner_reads': 'table sessions signed in this process from the restored generation: a signature '
                       'round trip that authorises the read and proves nothing about a printed sticker',
        'staff_reads': principal_note,
        'bearer': 'an original access token through the real authentication path, only while unexpired'}
    result['http_host'] = reads.host
    result['elapsed_s_local_not_rto'] = round(time.monotonic() - t0, 2)
    run['phase'] = 'done'


def check_providers_inert():
    """Both configured controls, verified on the LIVE objects rather than on the flags."""
    problems = []
    from django.core.mail import get_connection
    from django.core.mail.backends.locmem import EmailBackend
    try:
        if not isinstance(get_connection(), EmailBackend):
            problems.append('the e-mail backend in use is not the in-memory one')
    except Exception as exc:
        problems.append(f'the e-mail backend could not be resolved ({type(exc).__name__})')
    mongo = sys.modules.get('dinify_backend.mongo_db')
    if mongo is None:
        import importlib
        mongo = importlib.import_module('dinify_backend.mongo_db')
    try:
        mongo.MONGO_DB[MONGO_PROBE_COLLECTION]
        problems.append('the Mongo client answered; it is not inert')
    except RuntimeError:
        pass
    except Exception as exc:
        problems.append(f'the Mongo client failed unexpectedly ({type(exc).__name__})')
    client = getattr(getattr(mongo, '_lazy', None), '_client', 'unknown')
    if client is not None:
        problems.append('a Mongo client object exists')
    return check('FAIL' if problems else 'PASS', problems=problems,
                 note='SMTP, SMS and every other outbound provider are additionally unreachable by P.outbound_denied')


def check_db_readonly(conn):
    """``P.db_readonly``: PREVENTION — a role that cannot write, in two independent layers."""
    try:
        with conn.cursor() as c:
            c.execute("select current_user, (select rolsuper from pg_roles where rolname = current_user), "
                      "current_setting('default_transaction_read_only'), current_setting('transaction_read_only'), "
                      "current_setting('server_version_num')")
            user, superuser, default_ro, tx_ro, version = c.fetchone()
            # CASE, not AND, below: PostgreSQL does not promise to evaluate the relkind
            # filter before the privilege function, and has_sequence_privilege raises on a
            # relation that is not a sequence. No statement here takes parameters, so '%'
            # in a LIKE pattern is literal.
            c.execute("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                      "where n.nspname not in ('pg_catalog', 'information_schema') "
                      "and n.nspname not like 'pg\\_toast%' and "
                      "case when c.relkind in ('r', 'p', 'v', 'm', 'f') "
                      "then has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE') else false end")
            writable_tables = c.fetchone()[0]
            c.execute("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                      "where n.nspname not in ('pg_catalog', 'information_schema') and "
                      "case when c.relkind = 'S' then has_sequence_privilege(c.oid, 'USAGE,UPDATE') "
                      "else false end")
            writable_sequences = c.fetchone()[0]
            c.execute("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                      "where n.nspname not in ('pg_catalog', 'information_schema') "
                      "and n.nspname not like 'pg\\_toast%' and pg_get_userbyid(c.relowner) = current_user")
            owned = c.fetchone()[0]
            c.execute("select count(*) from pg_namespace where nspname not like 'pg\\_%' and "
                      "nspname <> 'information_schema' and has_schema_privilege(oid, 'CREATE')")
            create_schemas = c.fetchone()[0]
            c.execute("select has_database_privilege(current_database(), 'CREATE')")
            create_db = c.fetchone()[0]
    except Exception as exc:
        return check('FAIL', reason=f'the role could not be inspected ({type(exc).__name__})')
    ok = (superuser is False and default_ro == 'on' and tx_ro == 'on' and writable_tables == 0
          and writable_sequences == 0 and owned == 0 and create_schemas == 0 and not create_db)
    return check('PASS' if ok else 'FAIL', role=user, superuser=superuser,
                 default_transaction_read_only=default_ro, transaction_read_only=tx_ro,
                 writable_tables=writable_tables, writable_sequences=writable_sequences, owned_relations=owned,
                 schemas_with_create=create_schemas, database_create=create_db, server_version_num=version)


def check_db_exclusive(conn):
    """``P.db_exclusive``: no other client is connected to the target database now.

    Observed, and only meaningful when every session is VISIBLE: without
    ``pg_read_all_stats`` another role's row reads as NULL, and counting the visible ones
    would pass vacuously. An invisible session therefore FAILS the check.
    """
    try:
        with conn.cursor() as c:
            c.execute("select count(*) filter (where backend_type is null), "
                      "count(*) filter (where backend_type = 'client backend' and datname = current_database()) "
                      "from pg_stat_activity where pid <> pg_backend_pid()")
            invisible, others = c.fetchone()
    except Exception as exc:
        return check('FAIL', reason=f'pg_stat_activity could not be read ({type(exc).__name__})')
    if invisible:
        return check('FAIL', reason='some sessions are not visible to this role (grant pg_read_all_stats)',
                     invisible_sessions=invisible)
    return check('PASS' if others == 0 else 'FAIL', other_client_sessions=others,
                 note='a point-in-time observation; no_background_work is attested, not observed')


def database_facts(conn):
    with conn.cursor() as c:
        c.execute("select schemaname || '.' || sequencename, last_value from pg_sequences "
                  "where schemaname not in ('pg_catalog', 'information_schema') order by 1")
        sequences = [list(r) for r in c.fetchall()]
        c.execute('select pg_snapshot_xmax(pg_current_snapshot())::text')
        horizon = c.fetchone()[0]
        c.execute("select count(*) from pg_stat_activity where pid <> pg_backend_pid() "
                  "and backend_type = 'client backend' and datname = current_database()")
        others = c.fetchone()[0]
    return {'sequences': sequences, 'xact_horizon': horizon, 'other_clients': others}


def choose_sample(inputs, manifest):
    from orders_app.models import Order
    from restaurants_app.models import Restaurant
    lim = inputs['limits']
    if manifest is not None:
        return {'restaurant_ids': list(manifest['sample']['restaurant_ids']),
                'order_ids': list(manifest['sample']['order_ids'])}, 'manifest'
    rids = (inputs.get('sample') or {}).get('restaurant_ids')
    source, not_sampled = 'inputs', {'restaurants': 0, 'orders': 0}
    if not rids:
        source = 'discovered'
        live = Restaurant.objects.filter(deleted=False)
        rids = [str(r) for r in live.order_by('id').values_list('id', flat=True)[:lim['max_restaurants']]]
        not_sampled['restaurants'] = max(0, live.count() - len(rids))
    orders = []
    for rid in rids:
        rows = Order.objects.filter(restaurant_id=rid, deleted=False)
        picked = [str(o) for o in rows.order_by('-time_created', 'id')
                  .values_list('id', flat=True)[:lim['max_orders_per_restaurant']]]
        not_sampled['orders'] += max(0, rows.count() - len(picked))
        orders += picked
    # A SAMPLE, and the result says how much it left out: every I. check describes these
    # restaurants and orders, never the whole restore.
    return {'restaurant_ids': list(rids), 'order_ids': orders, 'not_sampled': not_sampled}, source


def resolve_principal(inputs):
    """The named staff principal, held to the refusals the customer JWT path applies.

    In-process authentication bypasses token verification, so those refusals are
    restated here rather than assumed.
    """
    from dinify_backend.configss.string_definitions import ACCOUNT_TYPE_RESTAURANT_USER
    from users_app.customer_access import is_established
    from users_app.models import User
    uid = (inputs.get('staff_principal') or {}).get('user_id')
    if not uid:
        return None, 'no staff principal supplied'
    user = User.objects.filter(pk=uid).first()
    if user is None:
        return None, 'the named principal is not in the restored data'
    if not user.is_active or getattr(user, 'account_type', None) != ACCOUNT_TYPE_RESTAURANT_USER \
            or not is_established(user):
        return None, 'the named principal is not an active, established restaurant user'
    return user, 'in-process principal (JWT verification NOT exercised)'


class Reads:
    """Every read is in process, through the ordinary URL resolver and middleware.

    A view that RAISES — a read that tried to write under the read-only role, say — is
    answered as the application answers it (the 500 handler) and recorded with the
    exception's type, so that read's check fails and the others still run.
    """

    def __init__(self, principal):
        from django.conf import settings
        from django.test import Client
        self.host = http_host(settings.ALLOWED_HOSTS)
        self.public = Client(SERVER_NAME=self.host, raise_request_exception=False)
        self.staff = None
        if principal is not None:
            from rest_framework.test import APIClient
            self.staff = APIClient(SERVER_NAME=self.host, raise_request_exception=False)
            self.staff.force_authenticate(user=principal)
        self.done = {}

    def get(self, key, client, path, params=None, **headers):
        # The local instants either side of the read: a clock-dependent answer (the public
        # menu's schedules) is held to what the policy allows at both ends.
        from django.utils import timezone as dj_timezone
        before = dj_timezone.localtime()
        resp = client.get(path, params or {}, **headers)
        after = dj_timezone.localtime()
        try:
            body = json.loads(resp.content.decode())
        except ValueError:
            body = {'_not_json': True}
        self.done[key] = {'status': resp.status_code, 'body': body, 'request_id': resp.headers.get('X-Request-ID'),
                          'window': (before, after)}
        exc = getattr(resp, 'exc_info', None)
        if exc and exc[0] is not None:
            self.done[key]['exception'] = f'{exc[0].__module__}.{exc[0].__name__}'
        return self.done[key]


def _covered(user, rid, module):
    if user is None:
        return False
    from users_app.controllers.permissions_check import can_user_access_module
    return bool(can_user_access_module(user, rid, module))


def read_plan(reads, inputs, sample, principal):
    """Issue every read once, in a fixed order. Returns what each check needs."""
    from dinify_backend.configss.string_definitions import MODULE_KITCHEN, MODULE_MENU, MODULE_SETTINGS
    from orders_app.models import Order
    from restaurants_app.controllers.diner_capability import issue_table_session
    from restaurants_app.models import MenuSection, Table
    lim = inputs['limits']
    plan = {'principal': principal, 'menu_cover': {}, 'kitchen_cover': {}, 'settings_cover': [],
            'sections_skipped': {}, 'orders': {}, 'media_refs': []}
    reads.get('readiness', reads.public, '/api/v1/health/ready/')
    for rid in sample['restaurant_ids']:
        reads.get(f'menu.public.{rid}', reads.public, '/api/v1/orders/journey/show-menu/', {'restaurant': rid})
        if _covered(principal, rid, MODULE_MENU):
            reads.get(f'staff.sections.{rid}', reads.staff, '/api/v1/restaurant-setup/menusections/',
                      {'restaurant': rid})
            sections = [str(s) for s in MenuSection.objects.filter(restaurant_id=rid, deleted=False)
                        .order_by('id').values_list('id', flat=True)]
            plan['menu_cover'][rid] = sections[:lim['max_sections_per_restaurant']]
            plan['sections_skipped'][rid] = max(0, len(sections) - lim['max_sections_per_restaurant'])
            for sid in plan['menu_cover'][rid]:
                reads.get(f'staff.items.{sid}', reads.staff, '/api/v1/restaurant-setup/menuitems/',
                          {'section': sid})
        if _covered(principal, rid, MODULE_KITCHEN):
            plan['kitchen_cover'][rid] = True
            reads.get(f'kitchen.active.{rid}', reads.staff, '/api/v1/kitchen/orders/active/', {'restaurant': rid})
            reads.get(f'kitchen.completed.{rid}', reads.staff, '/api/v1/kitchen/orders/completed/',
                      {'restaurant': rid})
        if _covered(principal, rid, MODULE_SETTINGS):
            plan['settings_cover'].append(rid)
    if plan['settings_cover']:
        reads.get('staff.restaurants', reads.staff, '/api/v1/restaurant-setup/restaurants/')
    for oid in sample['order_ids']:
        order = Order.objects.filter(pk=oid).select_related('table').first()
        entry = {'row': order is not None}
        plan['orders'][oid] = entry
        if order is None:
            continue
        table = order.table
        entry['scannable'] = table is not None and table.is_available_for_scan()
        if entry['scannable']:
            session = {'HTTP_X_DINER_SESSION': issue_table_session(table)}
            reads.get(f'order.details.{oid}', reads.public, '/api/v1/orders/journey/order-details/',
                      {'order': oid}, **session)
            if order.client_order_id:
                reads.get(f'order.details_by_intent.{oid}', reads.public, '/api/v1/orders/journey/order-details/',
                          {'intent': str(order.client_order_id)}, **session)
        if plan['kitchen_cover'].get(str(order.restaurant_id)):
            reads.get(f'kitchen.state.{oid}', reads.staff, f'/api/v1/kitchen/orders/{oid}/state/')
    originals = inputs.get('originals') or {}
    for rec in originals.get('printed_qr') or []:
        table = Table.objects.filter(pk=rec['table_id'], restaurant_id=rec['restaurant_id']).first()
        plan.setdefault('stickers', {})[rec['table_id']] = {
            'row': table is not None, 'scannable': table is not None and table.is_available_for_scan()}
        reads.get(f"qr.scan.{rec['table_id']}", reads.public, '/api/v1/orders/journey/table-scan/',
                  HTTP_X_DINER_CREDENTIAL=rec['credential'])
    token = originals.get('staff_bearer_token')
    if token:
        plan['token'] = token_status(token)
        if plan['token']['usable']:
            reads.get(BEARER_READ_KEY, reads.public, '/api/v1/restaurant-setup/restaurants/',
                      HTTP_AUTHORIZATION=f'Bearer {token}')
    plan['media_refs'] = media_references(sample['restaurant_ids'], lim['max_media_objects'])
    return plan


def token_status(token, now=None):
    """Evidence about the KEY, and only while unexpired a credential for one read."""
    import jwt
    from django.conf import settings
    conf = settings.SIMPLE_JWT
    try:
        claims = jwt.decode(token, conf['SIGNING_KEY'], algorithms=[conf['ALGORITHM']],
                            options={'verify_exp': False, 'verify_aud': False})
    except jwt.InvalidSignatureError:
        return {'key': 'FAIL', 'why': 'its signature does not verify under the configured key', 'usable': False}
    except Exception as exc:
        return {'key': 'FAIL', 'why': f'it is not a readable token ({type(exc).__name__})', 'usable': False}
    if claims.get(conf.get('TOKEN_TYPE_CLAIM', 'token_type')) != 'access':
        return {'key': 'PASS', 'why': 'its signature verifies; it is not an access token', 'usable': False,
                'incomplete': True}
    exp = claims.get('exp')
    now = time.time() if now is None else now
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return {'key': 'PASS', 'why': 'its signature verifies; it carries no usable expiry', 'usable': False,
                'incomplete': True}
    if exp <= now:
        return {'key': 'PASS', 'why': 'its signature verifies (key continuity); it has EXPIRED, so it '
                                      'authorises no read', 'usable': False}
    return {'key': 'PASS', 'why': 'its signature verifies (key continuity); it is unexpired', 'usable': True}


def media_references(restaurant_ids, cap):
    """The image references of the sampled restaurants, soft-deleted rows INCLUDED."""
    from django.db import connection
    refs = []
    if not restaurant_ids:
        return {'refs': refs, 'truncated': False}
    queries = (
        ('restaurants', 'logo', 'select id, deleted, logo from restaurants where id = any(%s::uuid[])'),
        ('restaurants', 'cover_photo', 'select id, deleted, cover_photo from restaurants where id = any(%s::uuid[])'),
        ('menu_sections', 'section_banner_image',
         'select id, deleted, section_banner_image from menu_sections where restaurant_id = any(%s::uuid[])'),
        ('menu_items', 'image', 'select i.id, i.deleted, i.image from menu_items i join menu_sections s '
                                'on s.id = i.section_id where s.restaurant_id = any(%s::uuid[])'),
    )
    with connection.cursor() as c:
        for table, column, sql in queries:
            c.execute(sql + ' order by 1', [list(restaurant_ids)])
            for rid, deleted, name in c.fetchall():
                if name:
                    refs.append({'table': table, 'column': column, 'row': str(rid), 'row_deleted': deleted,
                                 'name': name})
    # ``refs`` is the SAMPLE whose objects are read; ``all_names`` is every reference, so
    # a path the application emits can be classified exactly however many were sampled.
    return {'refs': refs[:cap], 'truncated': len(refs) > cap, 'total': len(refs),
            'all_names': sorted({r['name'] for r in refs})}


# ---------------------------------------------------------------------------- intrinsic

def intrinsic_checks(conn, reads, plan, inputs, principal_note):
    C = {'I.migrations': check_migrations(conn), 'I.readiness': check_readiness(reads.done.get('readiness'))}
    C['I.public_menu'] = check_public_menu(reads.done, plan_restaurants(reads.done))
    C.update(check_media(plan['media_refs'], reads.done))
    C['I.order_reads'] = check_order_reads(reads.done, plan['orders'])
    C['I.staff_reads'] = check_staff_reads(reads.done, plan, principal_note)
    C['I.kitchen_state'] = check_kitchen(reads.done, plan, principal_note)
    ids = {k: v.get('request_id') for k, v in reads.done.items()}
    from dinify_backend.request_context import is_request_id
    bad = sorted(k for k, v in ids.items() if not is_request_id(v))
    C['I.request_ids'] = check('PASS' if ids and not bad else ('INCOMPLETE' if not ids else 'FAIL'),
                               responses=len(ids), without_request_id=bad)
    return C


def plan_restaurants(done):
    return [k[len('menu.public.'):] for k in done if k.startswith('menu.public.')]


def check_migrations(conn):
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(conn, ignore_no_migrations=True)
    applied, nodes = set(loader.applied_migrations), set(loader.graph.nodes)
    pending = sorted(f'{a}.{n}' for a, n in nodes - applied)
    unknown = sorted(f'{a}.{n}' for a, n in applied - nodes)
    return check('PASS' if not pending and not unknown else 'FAIL', applied=len(applied), graph_nodes=len(nodes),
                 pending=pending[:20], applied_unknown_to_code=unknown[:20])


def check_readiness(read):
    if read is None:
        return check('INCOMPLETE', reason='the readiness read was not made')
    ok = read['status'] == 200 and read['body'] == {'status': 'ready', 'database': 'connected'}
    return check('PASS' if ok else 'FAIL', http=read['status'], body=read['body'],
                 note='a helper process, the same role, the same network namespace')


def expected_menu_answer(restaurant):
    """The public menu's contract for one restaurant, from the NAMED lifecycle constants.

    Returns (http status, exact body or None for a structural check). This restates the
    endpoint's contract; it does not change product policy.
    """
    from dinify_backend.configss.messages import ERR_RESTAURANT_UNAVAILABLE, MESSAGES
    from dinify_backend.configss.string_definitions import RESTAURANT_LIFECYCLE_STATES
    from restaurants_app.controllers.lifecycle_policy import (
        DINER_MENU_ALLOWED, DINER_MENU_GONE, DINER_MENU_UNAVAILABLE, diner_menu_visibility,
    )
    not_found = (404, {'status': 404, 'message': MESSAGES.get('RESTAURANT_NOT_FOUND')})
    if restaurant is None or restaurant.deleted:
        return not_found
    if restaurant.status not in RESTAURANT_LIFECYCLE_STATES:
        # The policy fails CLOSED on an unknown value (the menu is gone), so the endpoint's
        # 404 would "match". A restored row carrying a state the application does not know
        # is damage in its own right, never an acceptable answer.
        return None, None
    visibility = diner_menu_visibility(restaurant.status)
    if visibility == DINER_MENU_ALLOWED:
        return 200, None
    if visibility == DINER_MENU_UNAVAILABLE:
        return 503, {'status': 503, 'message': ERR_RESTAURANT_UNAVAILABLE}
    if visibility == DINER_MENU_GONE:
        return not_found
    return None, None


def expected_menu_content(restaurant_id, instant):
    """``{section id: {item id}}`` the public menu serves at ``instant``.

    Decided ONLY by the canonical publication policy (``menu_publication``) — the same
    predicates the endpoint applies — over the stored rows, never by a local restatement
    of what "published" means.
    """
    from restaurants_app.controllers.menu_publication import (
        item_visible_in_menu, section_operationally_visible,
    )
    from restaurants_app.models import MenuItem, MenuSection
    content = {}
    for section in MenuSection.objects.filter(restaurant_id=restaurant_id):
        if section_operationally_visible(section, instant):
            content[str(section.pk)] = set()
    items = (MenuItem.objects.filter(section_id__in=list(content))
             .select_related('section', 'section_group__section'))
    for item in items:
        if item_visible_in_menu(item, instant):
            content[str(item.section_id)].add(str(item.pk))
    return content


def menu_structure_problems(restaurant_id, body, window):
    """A served menu must be EXACTLY the visible menu, not merely a 200 of known ids.

    Returns ``(problems, incomplete, compared)``: ``compared`` counts the sections that
    were visible at BOTH ends of the read and so had to be served.
    """
    if not isinstance(body, dict) or body.get('status') != 200 or not isinstance(body.get('data'), list):
        return ['the body is not a menu envelope'], None, 0
    problems = []
    if 'upsell' not in body or not isinstance(body.get('item_sort_mode'), str):
        problems.append('the envelope is missing upsell/item_sort_mode')
    served = {}
    for i, section in enumerate(body['data']):
        if not isinstance(section, dict) or not isinstance(section.get('items'), list):
            problems.append(f'section[{i}] is malformed')
            continue
        sid = section.get('id')
        if sid in served:
            problems.append(f'section[{i}] is served twice')
            continue
        if section.get('item_count') != len(section['items']):
            problems.append(f'section[{i}] item_count disagrees with its items')
        ids = []
        for j, item in enumerate(section['items']):
            if not isinstance(item, dict) or item.get('section') != sid:
                problems.append(f'section[{i}].items[{j}] does not belong to that section')
                continue
            ids.append(item.get('id'))
        if len(ids) != len(set(ids)):
            problems.append(f'section[{i}] serves an item twice')
        served[sid] = set(ids)
    if not (isinstance(window, (tuple, list)) and len(window) == 2):
        return problems, 'the read carries no observation window, so the visible menu is not established', 0
    early, late = (expected_menu_content(restaurant_id, instant) for instant in window)
    must = {sid: early[sid] & late[sid] for sid in early.keys() & late.keys()}
    may = {sid: early.get(sid, set()) | late.get(sid, set()) for sid in early.keys() | late.keys()}
    not_served = set(must) - set(served)
    not_visible = set(served) - set(may)
    if not_served:
        problems.append(f'{len(not_served)} visible section(s) are not served')
    if not_visible:
        problems.append(f'{len(not_visible)} served section(s) are not visible')
    for sid, ids in served.items():
        if sid not in may:
            continue
        missing, hidden = must.get(sid, set()) - ids, ids - may[sid]
        if missing:
            problems.append(f'{len(missing)} visible item(s) of a served section are not served')
        if hidden:
            problems.append(f'{len(hidden)} served item(s) are not visible in that section')
    return problems, None, len(must)


def check_public_menu(done, restaurant_ids):
    from restaurants_app.models import Restaurant
    if not restaurant_ids:
        return check('INCOMPLETE', reason='no restaurant in the sample')
    rows, compared = {}, 0
    for rid in restaurant_ids:
        read = done[f'menu.public.{rid}']
        restaurant = Restaurant.objects.filter(pk=rid).first()
        if restaurant is None:
            rows[rid] = 'FAIL: the restaurant row is missing'
            continue
        status, body = expected_menu_answer(restaurant)
        if status is None:
            rows[rid] = f'FAIL: unknown lifecycle visibility for status {restaurant.status!r}'
        elif read['status'] != status:
            rows[rid] = f'FAIL: answered {read["status"]}, the contract for {restaurant.status!r} is {status}'
        elif body is not None:
            rows[rid] = 'PASS' if read['body'] == body else 'FAIL: the refusal body differs from the contract'
            compared += 1
        else:
            problems, incomplete, sections = menu_structure_problems(rid, read['body'], read.get('window'))
            compared += sections
            if problems:
                rows[rid] = 'FAIL: ' + '; '.join(problems[:5])
            elif incomplete:
                rows[rid] = f'INCOMPLETE: {incomplete}'
            else:
                rows[rid] = 'PASS'
    if any(v.startswith('FAIL') for v in rows.values()):
        return check('FAIL', restaurants=rows, compared=compared)
    if any(v.startswith('INCOMPLETE') for v in rows.values()) or not compared:
        return check('INCOMPLETE', restaurants=rows, compared=compared,
                     reason='no visible section or exact refusal was compared' if not compared else
                     'a menu could not be compared')
    return check('PASS', restaurants=rows, compared=compared)


def _media_paths(obj, prefix, out):
    if isinstance(obj, dict):
        for v in obj.values():
            _media_paths(v, prefix, out)
    elif isinstance(obj, list):
        for v in obj:
            _media_paths(v, prefix, out)
    elif isinstance(obj, str) and obj.startswith(prefix):
        out.add(obj[len(prefix):])
    return out


def read_media_object(name):
    """Storage interface + Django's static view under MEDIA_ROOT. Local evidence only."""
    from django.conf import settings
    from django.core.files.storage import default_storage
    from django.test import RequestFactory
    from django.views.static import serve
    from PIL import Image
    res = {'name': name}
    try:
        with default_storage.open(name, 'rb') as fh:
            data = fh.read()
        res['storage_sha256'] = sha256(data)
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            res['decodes'] = True
        except Exception as exc:
            res['decodes'] = f'no ({type(exc).__name__})'
    except Exception as exc:
        res['storage_sha256'] = None
        res['decodes'] = f'unreadable ({type(exc).__name__})'
    try:
        resp = serve(RequestFactory().get('/media/' + name), name, document_root=settings.MEDIA_ROOT)
        res['served_sha256'] = sha256(b''.join(resp.streaming_content))
    except Exception as exc:
        res['served_sha256'] = f'not served ({type(exc).__name__})'
    return res


def check_media(media, done):
    from django.conf import settings
    refs = media['refs']
    C = {}
    if not refs:
        C['I.media_references'] = check('INCOMPLETE', referenced=0,
                                        reason='the sample references no media; serving is unexercised')
    else:
        objects = [dict(read_media_object(r['name']), **{k: r[k] for k in ('table', 'column', 'row_deleted')})
                   for r in refs]
        bad = [o['name'] for o in objects if o['storage_sha256'] is None or o['decodes'] is not True
               or o['storage_sha256'] != o['served_sha256']]
        status = 'FAIL' if bad else ('INCOMPLETE' if media['truncated'] else 'PASS')
        C['I.media_references'] = check(status, referenced=media.get('total', len(refs)), checked=len(objects),
                                        truncated=media['truncated'], failing=bad[:50],
                                        evidence='storage interface + Django static view under MEDIA_ROOT; '
                                                 'NOT the deployed /media/ alias')
    # Every path the application emits is classified against the COMPLETE reference set
    # of the sampled restaurants, not only the sampled objects. If that set is absent and
    # the objects were sampled, a path outside the sample cannot be classified.
    known = set(media.get('all_names') or ()) | {r['name'] for r in refs}
    classifiable = 'all_names' in media or not media['truncated']
    seen = set()
    for key, read in done.items():
        if key.startswith('menu.'):
            _media_paths(read.get('body'), settings.MEDIA_URL, seen)
    unknown = sorted(seen - known)
    if not seen:
        status, reason = 'INCOMPLETE', 'the application emitted no media path; nothing was compared'
    elif unknown and not classifiable:
        status, reason = 'INCOMPLETE', 'a path outside the sampled references cannot be classified'
    else:
        status, reason = ('FAIL' if unknown else 'PASS'), None
    C['I.app_media_paths_are_db_references'] = check(
        status, paths=len(seen), references=len(known), not_referenced=unknown[:50],
        **({'reason': reason} if reason else {}))
    return C


def _line_key(line_id, quantity, name, deleted, with_deleted):
    return json.dumps([str(line_id), quantity, name] + ([deleted] if with_deleted else []), default=str)


def saved_lines(order):
    """What the order read must publish, from the SAVED rows, by the endpoint's own rules.

    ``items`` lists EVERY row, soft-deleted included, each with ALL its children;
    ``quote`` lists the live dishes, each with its live children — split by
    ``group_live_children``, the endpoint's own definition, never a local copy. Names are
    ``historical_name``: the name the line was bought under.
    Returns ``(items, quote, live_rows)``, each keyed by line id.
    """
    from orders_app.controllers.orders.serializers import historical_name
    from orders_app.controllers.services.order_quote import group_live_children
    from orders_app.models import OrderItem
    rows = list(OrderItem.objects.filter(order=order))
    live = [row for row in rows if not row.deleted]
    by_parent, _orphaned = group_live_children(live)
    children = {}
    for row in rows:
        if row.parent_item_id is not None:
            children.setdefault(row.parent_item_id, []).append(row)

    def key(row, with_deleted):
        return _line_key(row.pk, row.quantity, historical_name(row)[0], bool(row.deleted), with_deleted)

    items = {str(r.pk): (key(r, True), tuple(sorted(key(c, True) for c in children.get(r.pk, ()))))
             for r in rows}
    quote = {str(r.pk): (key(r, False), tuple(sorted(key(c, False) for c in by_parent.get(r.pk, ()))))
             for r in live if r.parent_item_id is None}
    return items, quote, live


def published_lines(entries, name_of, children_key, child_name_key, with_deleted):
    """The lines a read published, keyed by id: ``(lines, repeated)``, or ``(None, 0)``."""
    if not isinstance(entries, list):
        return None, 0
    lines, repeated = {}, 0
    for entry in entries:
        children = entry.get(children_key) if isinstance(entry, dict) else None
        if not isinstance(children, list) or not all(isinstance(c, dict) for c in children):
            return None, 0
        line_id = str(entry.get('id'))
        if line_id in lines:
            repeated += 1
            continue
        lines[line_id] = (
            _line_key(entry.get('id'), entry.get('quantity'), name_of(entry), entry.get('deleted'), with_deleted),
            tuple(sorted(_line_key(c.get('id'), c.get('quantity'), c.get(child_name_key), c.get('deleted'),
                                   with_deleted) for c in children)))
    return lines, repeated


def line_problems(label, saved, published, repeated):
    if published is None:
        return [f'{label}: not a list of lines']
    missing = saved.keys() - published.keys()
    unsaved = published.keys() - saved.keys()
    differing = [k for k in saved.keys() & published.keys() if saved[k] != published[k]]
    if missing or unsaved or differing or repeated:
        return [f'{label}: {len(missing)} saved line(s) missing, {len(unsaved)} not saved, '
                f'{len(differing)} differing, {repeated} repeated']
    return []


def order_read_problems(order, read, intent_read):
    from orders_app.controllers.services.acceptance_result import (
        ACCEPTANCE_ACCEPTED, ACCEPTANCE_EVIDENCE_UNAVAILABLE, ACCEPTANCE_NOT_ACCEPTED,
    )
    from dinify_backend.configss.string_definitions import OrderStatus_Initiated
    from orders_app.models import OrderAcceptance
    if read is None:
        return ['the read was not made']
    if read['status'] != 200 or not isinstance((read['body'] or {}).get('data'), dict):
        return [f'the read answered {read["status"]}']
    data = read['body']['data']
    checkout = data.get('checkout') if isinstance(data.get('checkout'), dict) else {}
    acceptance = checkout.get('acceptance') if isinstance(checkout.get('acceptance'), dict) else {}
    scope = checkout.get('scope') if isinstance(checkout.get('scope'), dict) else {}
    evidence = OrderAcceptance.objects.filter(order=order).first()
    if evidence is not None:
        expected = ACCEPTANCE_ACCEPTED
    elif order.order_status == OrderStatus_Initiated:
        expected = ACCEPTANCE_NOT_ACCEPTED
    else:
        expected = ACCEPTANCE_EVIDENCE_UNAVAILABLE
    problems = []
    if checkout.get('order_id') != str(order.pk) or data.get('id') != str(order.pk):
        problems.append('the read names a different order')
    if scope.get('restaurant') != str(order.restaurant_id) or scope.get('table') != str(order.table_id):
        problems.append('the read names a different scope')
    if acceptance.get('state') != expected:
        problems.append(f'the read says {acceptance.get("state")!r}, the stored evidence says {expected!r}')
    if acceptance.get('quote_ref') != (evidence.quote_ref if evidence is not None else None):
        problems.append('acceptance.quote_ref is not the stored reference')
    items, quote, live = saved_lines(order)
    if evidence is not None and not live:
        problems.append('an accepted order has no live line')
    item_name = (lambda e: e.get('item').get('name') if isinstance(e.get('item'), dict) else None)
    problems += line_problems('items', items,
                              *published_lines(data.get('items'), item_name, 'extra_items', 'name', True))
    problems += line_problems('quote', quote,
                              *published_lines(data.get('quote'), lambda e: e.get('item_name'), 'extras',
                                               'item_name', False))
    if intent_read is not None and (intent_read['status'] != 200 or intent_read['body'] != read['body']):
        problems.append('the intent selector disagrees with the order selector')
    return problems


def check_order_reads(done, orders):
    from orders_app.models import Order
    rows, covered = {}, 0
    for oid, entry in orders.items():
        if not entry['row']:
            rows[oid] = 'FAIL: the order row is missing'
            continue
        if not entry.get('scannable'):
            rows[oid] = 'NOT_COVERED: its table is not available for a diner session'
            continue
        covered += 1
        order = Order.objects.get(pk=oid)
        problems = order_read_problems(order, done.get(f'order.details.{oid}'),
                                       done.get(f'order.details_by_intent.{oid}'))
        rows[oid] = 'PASS' if not problems else 'FAIL: ' + '; '.join(problems)
    if any(v.startswith('FAIL') for v in rows.values()):
        return check('FAIL', orders=rows)
    if not covered:
        return check('INCOMPLETE', orders=rows, reason='no order in the sample could be read')
    return check('PASS' if covered == len(rows) else 'INCOMPLETE', orders=rows, covered=covered)


#: The staff list reads name no page, so the application answers page 1 at its paginator's
#: default size (``misc_app.controllers.paginator.DinifyPaginator``). That is the page
#: contract each read is held to; if the default moves, the read fails rather than a
#: different page being compared quietly.
STAFF_PAGE = 1
STAFF_PAGE_SIZE = 25
_ABSENT = object()


def endpoint_order_terms(model, prefix='', descending=False, depth=0):
    """``model``'s ``Meta.ordering`` as the database applies it to a staff list read.

    ``Secretary.read`` filters the model and keeps its default ordering. A relation term
    sorts by the related model's own ordering, recursively and with its direction carried,
    which is how Django expands it.
    """
    terms = []
    for term in model._meta.ordering:
        flip = term.startswith('-')
        name = term.lstrip('-')
        field = model._meta.pk if name == 'pk' else model._meta.get_field(name)
        desc = flip != descending
        if field.is_relation and field.related_model._meta.ordering and depth < 3:
            terms += endpoint_order_terms(field.related_model, f'{prefix}{name}__', desc, depth + 1)
        else:
            terms.append(('-' if desc else '') + prefix + name)
    return terms


def endpoint_ranks(queryset):
    """Each stored row's place in the endpoint's order; rows with equal sort keys share one.

    The database ranks the rows by the endpoint's own ordering (its collation, its NULL
    placement). Equal ranks are what tolerate ties: tied rows may come in either order, and
    when they straddle the end of the page, whichever of them the database put there is
    still page 1.
    """
    from django.db.models import Window
    from django.db.models.functions import DenseRank
    order = endpoint_order_terms(queryset.model)
    rows = queryset.annotate(restore_usability_rank=Window(DenseRank(), order_by=order)) \
        .values_list('id', 'restore_usability_rank')
    return {str(i): rank for i, rank in rows}


def _stated(value):
    return 'missing' if value is _ABSENT else repr(value)[:40]


def page_problems(read, queryset):
    """A staff list read is page 1 of the stored rows: complete for its size, in order.

    ``queryset`` is the population the endpoint lists. The page must hold exactly
    ``min(total, STAFF_PAGE_SIZE)`` distinct stored rows, the first ones in the endpoint's
    order and in that order, and its metadata must state page 1 of exactly that population. A record that is
    not a stored row, or appears twice, still fails as before. Missing or malformed
    metadata is a problem, never a default, and a further page is never read.
    """
    if read is None:
        return ['the read was not made']
    body = read['body'] or {}
    data = body.get('data') if isinstance(body, dict) else None
    if read['status'] != 200 or not isinstance(data, dict) or not isinstance(data.get('records'), list):
        return [f'answered {read["status"]} without a record page']
    ranks = endpoint_ranks(queryset)
    total = len(ranks)
    ids = [r.get('id') if isinstance(r, dict) and isinstance(r.get('id'), str) else None for r in data['records']]
    problems = []
    if any(i not in ranks for i in ids) or len(set(ids)) != len(ids):
        problems.append('a record is not one of the stored rows it should be drawn from')
    size = min(total, STAFF_PAGE_SIZE)
    if len(ids) != size:
        problems.append(f'page {STAFF_PAGE} holds {len(ids)} record(s); of the {total} stored it must hold {size}')
    elif not problems and [ranks[i] for i in ids] != sorted(ranks.values())[:size]:
        problems.append(f"the records are not page {STAFF_PAGE} in the endpoint's order")
    pagination = data.get('pagination')
    if not isinstance(pagination, dict):
        return problems + ['the page carries no pagination metadata']
    stated = {'paginated': True, 'total_records': total, 'page_size': STAFF_PAGE_SIZE,
              'current_page': STAFF_PAGE, 'number_of_pages': max(1, -(-total // STAFF_PAGE_SIZE)),
              'has_next': total > STAFF_PAGE * STAFF_PAGE_SIZE, 'has_previous': STAFF_PAGE > 1}
    for key, want in stated.items():
        got = pagination.get(key, _ABSENT)
        typed = isinstance(got, bool) if isinstance(want, bool) else \
            isinstance(got, int) and not isinstance(got, bool)
        if not typed or got != want:
            problems.append(f'pagination {key} is {_stated(got)}; page {STAFF_PAGE} of {total} states {want!r}')
    return problems


def check_staff_reads(done, plan, principal_note):
    from restaurants_app.models import MenuItem, MenuSection
    from users_app.controllers.permissions_check import get_module_restaurant_ids
    if not principal_note.startswith('in-process'):
        return check('UNAVAILABLE', reason=principal_note)
    rows = {}
    for rid, sections in plan['menu_cover'].items():
        problems = page_problems(done.get(f'staff.sections.{rid}'),
                                 MenuSection.objects.filter(restaurant_id=rid, deleted=False))
        for sid in sections:
            problems += [f'section {sid}: {p}' for p in page_problems(
                done.get(f'staff.items.{sid}'), MenuItem.objects.filter(section_id=sid, deleted=False))]
        if plan['sections_skipped'].get(rid):
            problems.append(f'INCOMPLETE: {plan["sections_skipped"][rid]} section(s) past the limit were not read')
        rows[rid] = 'PASS' if not problems else '; '.join(problems[:5])
    if plan['settings_cover']:
        from dinify_backend.configss.string_definitions import MODULE_SETTINGS
        from restaurants_app.models import Restaurant
        allowed = get_module_restaurant_ids(plan['principal'], MODULE_SETTINGS)
        problems = page_problems(done.get('staff.restaurants'),
                                 Restaurant.objects.filter(id__in=allowed, deleted=False))
        rows['restaurants'] = 'PASS' if not problems else '; '.join(problems)
    if not rows:
        return check('INCOMPLETE', reason='the principal can read no sampled restaurant', authorisation=principal_note)
    if any(not v.startswith(('PASS', 'INCOMPLETE')) for v in rows.values()):
        return check('FAIL', restaurants=rows, authorisation=principal_note)
    status = 'PASS' if all(v == 'PASS' for v in rows.values()) else 'INCOMPLETE'
    return check(status, restaurants=rows, authorisation=principal_note)


def kitchen_feed_orders(rid, feed, window):
    """The stored orders a kitchen feed MUST and MAY serve, by its own endpoint predicate.

    Restates the two querysets in ``orders_app.endpoints_kitchen`` (the views hold them
    inline). The active feed is clock-free, so the two sets are one. The completed feed
    keeps an order while ``served_at >= now - COMPLETED_WINDOW`` for the endpoint's
    ``now``, an instant inside the read: an order inside the window at BOTH ends of the
    read must be served, one outside it at both ends must not be, and one that crossed
    the boundary during the read may be either.
    """
    from django.db.models import Q
    from dinify_backend.configss.string_definitions import OrderStatus_Cancelled, OrderStatus_Initiated
    from orders_app.endpoints_kitchen import COMPLETED_WINDOW
    from orders_app.models import Order
    if feed == 'active':
        rows = Order.objects.filter(~Q(fulfilment_status='served'), deleted=False, restaurant=rid) \
            .exclude(order_status=OrderStatus_Cancelled).exclude(order_status=OrderStatus_Initiated)
        ids = {str(i) for i in rows.values_list('id', flat=True)}
        return ids, ids
    # As instants (UTC), as the endpoint's ``timezone.now()`` is: wall-clock arithmetic on a
    # local time would move the bound by an hour across a daylight-saving change.
    before, after = (t.astimezone(timezone.utc) for t in window)
    rows = Order.objects.filter(fulfilment_status='served', deleted=False, restaurant=rid) \
        .exclude(order_status=OrderStatus_Cancelled)
    must = {str(i) for i in rows.filter(served_at__gte=after - COMPLETED_WINDOW).values_list('id', flat=True)}
    may = {str(i) for i in rows.filter(served_at__gte=before - COMPLETED_WINDOW).values_list('id', flat=True)}
    return must, may


def _read_window(read):
    window = read.get('window')
    if isinstance(window, tuple) and len(window) == 2 and all(isinstance(t, datetime) and t.tzinfo
                                                                for t in window) and window[0] <= window[1]:
        return window
    return None


def feed_problems(read, rid, feed):
    """One kitchen feed held to the stored orders its endpoint selects.

    Every ticket must be an order this feed serves, served once, and agree with the stored
    row on the identity/status projection; every order the feed must serve must be there.
    Returns the problems and how many tickets were compared.
    """
    from orders_app.models import Order
    body = (read or {}).get('body')
    data = body.get('data') if isinstance(body, dict) else None
    if read is None or read['status'] != 200 or not isinstance(data, list):
        return [f'{feed} feed answered {(read or {}).get("status")}'], 0
    window = _read_window(read)
    if feed == 'completed' and window is None:
        return ['the completed feed was read with no recorded window'], 0
    must, may = kitchen_feed_orders(rid, feed, window)
    tickets, counts = {}, {'unreadable': 0, 'repeated': 0, 'not_this_feed': 0}
    for ticket in data:
        tid = ticket.get('id') if isinstance(ticket, dict) else None
        if not _is_uuid(tid):
            counts['unreadable'] += 1
        elif tid in tickets:
            counts['repeated'] += 1
        elif tid not in may:
            tickets[tid] = None
            counts['not_this_feed'] += 1
        else:
            tickets[tid] = ticket
    fields = ('fulfilment_revision', 'fulfilment_status', 'order_status')
    stored = {str(row['id']): row for row in Order.objects.filter(pk__in=[t for t, v in tickets.items() if v])
              .values('id', *fields)}
    disagree = sum(1 for tid, ticket in tickets.items() if ticket is not None
                   and any(type(ticket.get(f)) is not type(stored[tid][f]) or ticket.get(f) != stored[tid][f]
                           for f in fields))
    missing = len(must - set(tickets))
    problems = [f'{n} {feed} ticket(s) {what}' for n, what in (
        (counts['unreadable'], 'carry no order id'),
        (counts['repeated'], 'are served twice'),
        (counts['not_this_feed'], f'are not orders the {feed} feed serves'),
        (disagree, 'disagree with the stored order'),
        (missing, f'are missing: stored orders the {feed} feed must serve')) if n]
    compared = sum(1 for ticket in tickets.values() if ticket is not None)
    return problems, (0 if problems else compared)


def check_kitchen(done, plan, principal_note):
    from orders_app.models import Order
    if not principal_note.startswith('in-process'):
        return check('UNAVAILABLE', reason=principal_note)
    rows, covered, tickets = {}, 0, 0
    for rid in plan['kitchen_cover']:
        problems, compared = [], 0
        for feed in ('active', 'completed'):
            found, n = feed_problems(done.get(f'kitchen.{feed}.{rid}'), rid, feed)
            problems += found
            compared += n
        tickets += compared
        # A feed with nothing to serve that serves nothing is right, and compares nothing.
        rows[f'feeds.{rid}'] = 'FAIL: ' + '; '.join(problems) if problems else \
            (f'PASS: {compared} ticket(s)' if compared else 'EMPTY: neither feed holds an order')
    for oid, entry in plan['orders'].items():
        read = done.get(f'kitchen.state.{oid}')
        if read is None:
            continue
        covered += 1
        order = Order.objects.filter(pk=oid).first()
        data = (read.get('body') or {}).get('data') if isinstance(read.get('body'), dict) else None
        fields = ('fulfilment_revision', 'fulfilment_status', 'order_status', 'priority')
        if order is None or read['status'] != 200 or not isinstance(data, dict) or data.get('id') != oid \
                or any(data.get(f) != getattr(order, f) for f in fields):
            rows[f'state.{oid}'] = 'FAIL: the kitchen state disagrees with the stored order'
        else:
            rows[f'state.{oid}'] = 'PASS'
    if not plan['kitchen_cover']:
        return check('INCOMPLETE', reason='the principal can read no sampled kitchen', authorisation=principal_note)
    if any(v.startswith('FAIL') for v in rows.values()):
        return check('FAIL', rows=rows)
    if not tickets and not covered:
        return check('INCOMPLETE', rows=rows, reason='no feed ticket and no order state was compared')
    if not covered and plan['orders']:
        return check('INCOMPLETE', rows=rows, reason='no sampled order was read from the kitchen')
    return check('PASS', rows=rows, tickets_compared=tickets, orders_compared=covered)


def order_observations(sample):
    from orders_app.controllers.services.order_quote import quote_ref
    from orders_app.models import Order, OrderAcceptance, OrderItem
    same_ref, same_sum, compared = 0, 0, 0
    for oid in sample['order_ids']:
        order = Order.objects.filter(pk=oid).first()
        if order is None:
            continue
        compared += 1
        evidence = OrderAcceptance.objects.filter(order=order).first()
        if evidence is not None and evidence.quote_ref == quote_ref(order):
            same_ref += 1
        live = OrderItem.objects.filter(order=order, deleted=False)
        if sum((x.actual_cost for x in live), start=0) == order.actual_cost:
            same_sum += 1
    return {
        'O.acceptance_ref_recomputes': check('OBSERVED', equal=same_ref, orders=compared,
                                             note='holds only while lines are unchanged since acceptance'),
        'O.line_sum_equals_payable': check('OBSERVED', equal=same_sum, orders=compared,
                                           note='holds as of the last reconcile'),
    }


# ---------------------------------------------------------------------------- independent originals

def independent_checks(inputs, reads, plan):
    orig = inputs.get('originals') or {}
    C = {}
    C['H.printed_qr'] = check_stickers(orig.get('printed_qr'), reads.done, plan.get('stickers') or {}) \
        if orig.get('printed_qr') else check('UNAVAILABLE', reason='no original sticker credentials supplied')
    C['H.orders'] = check_original_orders(orig.get('orders')) if orig.get('orders') \
        else check('UNAVAILABLE', reason='no original order records supplied')
    token = plan.get('token')
    if token is None:
        C['H.key_continuity'] = check('UNAVAILABLE', reason='no original bearer token supplied')
        C['H.bearer_read'] = check('UNAVAILABLE', reason='no original bearer token supplied')
    else:
        C['H.key_continuity'] = check(token['key'], reason=token['why'],
                                      keys_checked=['SIMPLE_JWT.SIGNING_KEY'])
        if token['usable']:
            read = reads.done.get(BEARER_READ_KEY) or {}
            body = read.get('body') if isinstance(read.get('body'), dict) else {}
            ok = read.get('status') == 200 and isinstance((body.get('data') or {}).get('records'), list)
            C['H.bearer_read'] = check('PASS' if ok else 'FAIL', http=read.get('status'))
        else:
            C['H.bearer_read'] = check('INCOMPLETE' if token.get('incomplete') else 'UNAVAILABLE',
                                       reason=f'the token authorises no read ({token["why"]})')
    return C


def check_stickers(records, done, stickers):
    """An ORIGINAL sticker must resolve to the table AND the restaurant it was printed for.

    HTTP 200 alone is not identity: two valid stickers swapped between table keys both
    answer 200 and both name the wrong table.
    """
    rows = {}
    for rec in records:
        tid, rid = rec['table_id'], rec['restaurant_id']
        read = done.get(f'qr.scan.{tid}') or {}
        facts = stickers.get(tid) or {}
        data = (read.get('body') or {}).get('data') if isinstance(read.get('body'), dict) else None
        if not facts.get('row'):
            rows[tid] = 'FAIL: the table it was printed for is not in the restored data'
        elif not facts.get('scannable'):
            rows[tid] = 'INCOMPLETE: that table is not available for scanning in the restored data'
        elif read.get('status') != 200 or not isinstance(data, dict):
            rows[tid] = f'FAIL: the original credential was refused ({read.get("status")})'
        elif data.get('id') != tid or (data.get('restaurant') or {}).get('id') != rid:
            rows[tid] = 'FAIL: the credential resolves to a different table or restaurant'
        else:
            rows[tid] = 'PASS'
    if any(v.startswith('FAIL') for v in rows.values()):
        return check('FAIL', tables=rows)
    return check('PASS' if all(v == 'PASS' for v in rows.values()) else 'INCOMPLETE', tables=rows,
                 note='interpret against rotation history: a restore re-accepts stickers revoked after its point')


def check_original_orders(records):
    from orders_app.models import Order, OrderAcceptance
    rows = {}
    for rec in records:
        oid = rec['order_id']
        order = Order.objects.filter(pk=oid).first()
        problems = []
        if order is None:
            problems.append('the order is not in the restored data')
        else:
            if str(order.restaurant_id) != rec['restaurant_id'] or str(order.table_id) != rec['table_id']:
                problems.append('it belongs to a different restaurant or table')
            if rec['state'] == 'accepted':
                evidence = OrderAcceptance.objects.filter(order=order).first()
                if evidence is None:
                    problems.append('the original says accepted; the restore holds no acceptance')
                elif evidence.quote_ref != rec['quote_ref']:
                    problems.append('the accepted quote reference differs from the original')
        rows[oid] = 'PASS' if not problems else 'FAIL: ' + '; '.join(problems)
    return check('PASS' if all(v == 'PASS' for v in rows.values()) else 'FAIL', orders=rows,
                 compared=len(rows))


# ---------------------------------------------------------------------------- manifest identity

def identity_reads(reads):
    applied = set()
    out = {}
    for key, read in reads.done.items():
        if identity_read_key(key):
            out[key] = {'status': read['status'], 'body': apply_transformations(key, read['body'], applied)}
    return out, sorted(applied)


def table_digests(conn):
    out = {}
    with conn.cursor() as c:
        c.execute("select tablename from pg_tables where schemaname = 'public' order by tablename")
        for (name,) in c.fetchall():
            c.execute('select count(*), encode(sha256(convert_to(coalesce(string_agg(x, E\'\\n\' '
                      f'order by x collate "C"), \'\'), \'UTF8\')), \'hex\') from (select t::text as x from "{name}" t) s')
            rows, digest = c.fetchone()
            out[name] = {'rows': rows, 'sha256': digest}
    return out


def media_listing(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            path = os.path.join(dirpath, fn)
            with open(path, 'rb') as fh:
                data = fh.read()
            out[os.path.relpath(path, root)] = {'size': len(data), 'sha256': sha256(data)}
    return dict(sorted(out.items()))


def identity_checks(manifest, conn, executed, media, result):
    if manifest is None:
        return {k: check('UNAVAILABLE', reason='no backup-time manifest supplied')
                for k in ('R.source', 'R.tables', 'R.media_set', 'R.media_bytes', 'R.reads')}
    from django.conf import settings
    C = {}
    running = (result['checks'].get('P.source_identity') or {}).get('revision')
    C['R.source'] = check('PASS' if running and manifest['source_revision'] == running else 'FAIL',
                          manifest=manifest['source_revision'], running=running)
    tables = table_digests(conn)
    want = manifest['tables']
    differing = sorted(t for t in set(tables) | set(want) if tables.get(t) != want.get(t))
    C['R.tables'] = check('PASS' if not differing else 'FAIL', tables=len(tables), differing=differing[:20])
    listing = media_listing(settings.MEDIA_ROOT)
    recorded = manifest['media_listing']
    C['R.media_set'] = check('PASS' if listing == recorded else 'FAIL',
                             missing=sorted(set(recorded) - set(listing))[:20],
                             extra=sorted(set(listing) - set(recorded))[:20],
                             changed=sorted(k for k in set(listing) & set(recorded) if listing[k] != recorded[k])[:20])
    names = sorted({r['name'] for r in media['refs']})
    if not names:
        C['R.media_bytes'] = check('INCOMPLETE', reason='the sample references no media')
    else:
        mismatch = [n for n in names if (recorded.get(n) or {}).get('sha256') != (listing.get(n) or {}).get('sha256')]
        C['R.media_bytes'] = check('FAIL' if mismatch else ('INCOMPLETE' if media['truncated'] else 'PASS'),
                                   compared=len(names), mismatch=mismatch[:20])
    C['R.reads'] = compare_golden(manifest['golden_reads'], executed)
    return C


if __name__ == '__main__':
    # Run the CANONICAL module, not this ``__main__`` copy, so the bootstrap and the
    # adapter share one registry of issued handovers.
    from misc_app import restore_usability as _canonical
    sys.exit(_canonical.main())
