"""
What this app's MongoDB layer does when the server is unavailable, and the
stand-ins other suites install to reproduce it.

``dinify_backend/test_settings.py`` replaces ``dinify_backend.mongo_db`` with a
``MagicMock``. A ``MagicMock`` answers every call, iterates as empty and never
raises, so nothing in the suite ever met a real driver failure. Two callers
iterated a cursor outside their try/except and passed every test for exactly that
reason: the first-time menu approval's submitter lookup and the ``send_messages``
drain.

pymongo's ``Collection.find()`` performs no I/O. It returns a lazy ``Cursor``,
and the query runs on the first iteration. Against a server the client can name
but not reach, the failure therefore surfaces at the ITERATION, after the app's
2 s ``serverSelectionTimeoutMS``, never at ``find()``. A try/except around
``find()`` alone does not see it.

There are two unavailable states, and they fail at different points:

* UNREACHABLE. The client can be built (a ``mongodb://`` host, or a
  ``mongodb+srv://`` name that resolves) but no server answers. ``MONGO_DB[...]``
  and ``find()`` succeed; iterating the cursor raises
  ``ServerSelectionTimeoutError``. ``insert_one`` and ``update_one`` raise the
  same error.
* UNCONFIGURED. The client cannot be built (an unparseable URI, or a
  ``mongodb+srv://`` name that does not resolve). The app's own proxy then
  raises ``RuntimeError`` at ``MONGO_DB[...]`` itself, before any cursor exists.

``PremiseTests`` drives the REAL ``dinify_backend/mongo_db.py`` and the real
driver to establish both, against a closed loopback port and an unparseable URI,
so it needs no MongoDB server and sends nothing off the machine.
``FakeFidelityTests`` checks the stand-ins against the same premise, so a driver
change that made ``find()`` eager would fail here rather than leave the other
suites testing a fiction.
"""
import copy
import importlib.util
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bson import ObjectId
from django.test import SimpleTestCase
from pymongo.cursor import Cursor
from pymongo.errors import ServerSelectionTimeoutError

ROOT = Path(__file__).resolve().parent.parent
MONGO_MODULE = ROOT / 'dinify_backend' / 'mongo_db.py'


def _closed_loopback_port():
    """A loopback port nothing listens on, so a connection is refused at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@contextmanager
def real_mongo_module(host):
    """The REAL ``dinify_backend/mongo_db.py``, configured with ``host``.

    It is loaded as a fresh module object rather than through ``sys.modules``,
    where the test settings keep their mock. The host is read when the client is
    first used, so the environment stays patched for the whole block, and the
    client is closed on the way out.
    """
    spec = importlib.util.spec_from_file_location('dinify_backend.mongo_db', MONGO_MODULE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {'MONGO_HOST': host, 'MONGO_DATABASE': 'dinify_unavailability_test'}):
        spec.loader.exec_module(module)
        try:
            yield module
        finally:
            client = module._lazy._client
            if client is not None:
                client.close()


@contextmanager
def unreachable_real_mongo():
    """The app's own client, pointed at a server it can name but not reach."""
    with real_mongo_module(f'mongodb://127.0.0.1:{_closed_loopback_port()}') as module:
        yield module


# --------------------------------------------------------------------------
# Stand-ins. Each records the operations it was asked for in ``calls``, as
# ``(collection, operation)`` pairs, so a suite can assert what was NOT asked.
# --------------------------------------------------------------------------

class _LazyCursor:
    """A cursor that evaluates on first iteration, as pymongo's does."""

    def __init__(self, evaluate):
        self._evaluate = evaluate
        self._rows = None

    def __iter__(self):
        return self

    def __next__(self):
        if self._rows is None:
            self._rows = iter(self._evaluate())
        return next(self._rows)


def _unreachable_error(collection, operation):
    return ServerSelectionTimeoutError(
        f'127.0.0.1:1: [Errno 111] Connection refused, Timeout: 2.0s '
        f'(test stand-in: {collection}.{operation})'
    )


class _UnreachableCollection:

    def __init__(self, calls, name):
        self._calls = calls
        self._name = name

    def find(self, *args, **kwargs):
        self._calls.append((self._name, 'find'))
        error = _unreachable_error(self._name, 'find')

        def evaluate():
            raise error

        return _LazyCursor(evaluate)

    def _refuse(self, operation):
        self._calls.append((self._name, operation))
        raise _unreachable_error(self._name, operation)

    def insert_one(self, *args, **kwargs):
        self._refuse('insert_one')

    def update_one(self, *args, **kwargs):
        self._refuse('update_one')

    def find_one(self, *args, **kwargs):
        self._refuse('find_one')


class UnreachableMongo:
    """``MONGO_DB`` when the client was built and no server answers."""

    def __init__(self):
        self.calls = []

    def __getitem__(self, name):
        return _UnreachableCollection(self.calls, name)


class UnconfiguredMongo:
    """``MONGO_DB`` when the client could not be built: the app's proxy refuses
    at subscription, with the app's own message."""

    def __init__(self):
        self.calls = []

    def __getitem__(self, name):
        self.calls.append((name, '__getitem__'))
        raise RuntimeError(f"MongoDB is not available. Cannot access collection '{name}'.")


def _resolve(document, dotted):
    value = document
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _matches(document, query):
    """Exact equality on (dotted) keys, plus ``$exists``. Anything else is
    refused, so a test written against an operator this does not model fails
    loudly instead of matching the wrong documents."""
    for key, expected in (query or {}).items():
        present, value = _resolve(document, key)
        if isinstance(expected, dict) and any(k.startswith('$') for k in expected):
            if set(expected) != {'$exists'}:
                raise NotImplementedError(f'operator not modelled: {sorted(expected)}')
            if present != bool(expected['$exists']):
                return False
            continue
        if not present or value != expected:
            return False
    return True


class _MemoryCollection:

    def __init__(self, calls, name):
        self._calls = calls
        self.name = name
        self.documents = []

    def insert_one(self, document):
        self._calls.append((self.name, 'insert_one'))
        stored = copy.deepcopy(document)
        stored.setdefault('_id', ObjectId())
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored['_id'])

    def find(self, filter=None, *args, **kwargs):
        self._calls.append((self.name, 'find'))
        query = copy.deepcopy(filter or {})
        return _LazyCursor(
            lambda: [copy.deepcopy(d) for d in self.documents if _matches(d, query)]
        )

    def update_one(self, filter, update):
        self._calls.append((self.name, 'update_one'))
        if set(update) != {'$set'}:
            raise NotImplementedError(f'update not modelled: {sorted(update)}')
        for document in self.documents:
            if _matches(document, filter):
                document.update(copy.deepcopy(update['$set']))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class InMemoryMongo:
    """``MONGO_DB`` when the server answers. Stores what it is given."""

    def __init__(self):
        self.calls = []
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = _MemoryCollection(self.calls, name)
        return self.collections[name]


class _InlineThread:
    """``threading.Thread`` for code that writes from a daemon thread: runs the
    target when started, so its write has happened (or failed) by the time the
    caller returns."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


@contextmanager
def action_log_store(store):
    """Install ``store`` where ``save_action`` writes, and run its write inline.

    ``save_action`` resolves ``MONGO_DB`` from its own module namespace when the
    write runs, so that is the name patched. Only that module's ``threading`` is
    replaced, never the process-wide one.
    """
    with mock.patch('misc_app.controllers.save_action_log.MONGO_DB', store), \
            mock.patch('misc_app.controllers.save_action_log.threading',
                       SimpleNamespace(Thread=_InlineThread)):
        yield store


class PremiseTests(SimpleTestCase):
    """The real module and the real driver, with no MongoDB server anywhere."""

    def test_UNREACHABLE_a_guard_around_find_alone_does_not_catch_it(self):
        """The shape both callers had: ``find()`` inside the try, the iteration
        after it. The guard is satisfied, and the error arrives one statement
        later, after the app's 2 s selection timeout."""
        with unreachable_real_mongo() as module:
            try:
                cursor = module.MONGO_DB['action_logs'].find({'model': 'anything'})
            except Exception as exc:  # pragma: no cover - the premise is that this is not reached
                self.fail(f'find() raised {exc!r}; the lazy-cursor premise is wrong')
            self.assertIsInstance(
                cursor, Cursor,
                'find() returned without contacting a server: the client is built '
                'and the query has not run',
            )
            with self.assertRaises(ServerSelectionTimeoutError):
                list(cursor)

    def test_UNCONFIGURED_the_proxy_refuses_before_any_cursor_exists(self):
        with real_mongo_module('unconfigured-test://nowhere') as module, \
                self.assertLogs('dinify_backend.mongo_db', level='ERROR'):
            with self.assertRaisesRegex(RuntimeError, 'MongoDB is not available'):
                module.MONGO_DB['action_logs']


class FakeFidelityTests(SimpleTestCase):
    """The stand-ins fail where, and as, the real layer does."""

    def test_the_unreachable_stand_in_is_lazy_like_the_driver(self):
        store = UnreachableMongo()
        cursor = store['action_logs'].find({'model': 'anything'})
        with self.assertRaises(ServerSelectionTimeoutError):
            list(cursor)
        with self.assertRaises(ServerSelectionTimeoutError):
            store['action_logs'].insert_one({'model': 'anything'})
        with self.assertRaises(ServerSelectionTimeoutError):
            store['notifications'].update_one({'_id': 1}, {'$set': {'sent': True}})

    def test_the_unconfigured_stand_in_refuses_with_the_apps_own_message(self):
        self.assertIn(
            "MongoDB is not available. Cannot access collection '",
            MONGO_MODULE.read_text(),
            'the app proxy no longer says what the stand-in says',
        )
        with self.assertRaisesRegex(RuntimeError, "MongoDB is not available. Cannot access collection 'x'."):
            UnconfiguredMongo()['x']

    def test_the_in_memory_stand_in_matches_dotted_keys_and_exists(self):
        store = InMemoryMongo()
        store['c'].insert_one({'model': 'm', 'user': {'id': 'u1'}})
        store['c'].insert_one({'model': 'm', 'user': {'id': 'u2'}, 'sent': True})
        self.assertEqual(len(list(store['c'].find({'user.id': 'u1'}))), 1)
        self.assertEqual(len(list(store['c'].find({'sent': {'$exists': False}}))), 1)
        self.assertEqual(len(list(store['c'].find({'model': 'other'}))), 0)
        with self.assertRaises(NotImplementedError):
            list(store['c'].find({'n': {'$gt': 1}}))
