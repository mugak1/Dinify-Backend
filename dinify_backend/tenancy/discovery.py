"""
Discovery of writable relational fields across every project ``ModelSerializer``
(TENANT-STRUCT-00).

A serializer built with ``fields='__all__'`` (or an explicit list that includes a
relation without ``read_only``) exposes that FK/O2O/M2M as WRITABLE from the
request body. This module enumerates every such (serializer, field) pair by
INTROSPECTING the instantiated serializer's ``.fields`` — not by scanning source
text for fields — so a relation inherited from a parent serializer is still found.
The tenancy meta-test and the baseline generator both build on this.

Discovery completeness: ``__subclasses__()`` only sees classes already imported,
so a serializer must be imported before it can be introspected. We find which
modules to import by AST (``_serializer_defining_modules``) — any project module
whose SOURCE defines a class with a ``*Serializer`` base — NOT by the module's
name. A ``ModelSerializer`` hidden in ``views.py`` / ``api.py`` is therefore still
discovered; a module that DEFINES a serializer but cannot be imported is surfaced
in ``import_serializer_modules()``'s failure map and fails the meta-test closed
(see ``KNOWN_UNIMPORTABLE``). This closes the earlier gap where discovery only
imported modules whose path contained the substring ``"serializer"``.

Imported ONLY by tooling/tests; never by a request path.
"""
import ast
import importlib
import logging
from pathlib import Path

from django.apps import apps
from rest_framework.relations import ManyRelatedField, RelatedField
from rest_framework.serializers import ModelSerializer

logger = logging.getLogger(__name__)

# Serializers whose ``.fields`` cannot be introspected with a bare ``cls()``.
# The meta-test asserts the discovered-un-introspectable set EQUALS this, so a
# NEW un-introspectable serializer fails loudly (it can't silently hide its
# relations) and fixing a listed one reminds you to shrink this set.
KNOWN_UNINTROSPECTABLE = frozenset()

# Project modules that DEFINE a serializer (by AST) but cannot be imported in the
# tooling/test context. The meta-test asserts the discovered import-failure set
# EQUALS this, so a NEWLY-undiscoverable serializer module fails loudly — a
# serializer that can't be imported can't be introspected and must not silently
# escape the ratchet. Empty by design: every serializer module must import. Add an
# entry only with an explicit justification (and prefer fixing the import).
KNOWN_UNIMPORTABLE = frozenset()


def _class_key(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def field_key(cls, field_name: str) -> str:
    """Stable key for a (serializer, field) pair, used by the baseline file."""
    return f"{_class_key(cls)}::{field_name}"


def _project_app_configs():
    """Project (non-site-packages) app configs."""
    for config in apps.get_app_configs():
        if "site-packages" in str(Path(config.path)):
            continue  # third-party / django.contrib app
        yield config


def _module_defines_serializer(source: str) -> bool:
    """
    True iff the module SOURCE defines a class whose ANY base name contains
    ``"Serializer"`` — a pure AST check, no import. Catches ``ModelSerializer``
    subclasses and custom ``*Serializer`` bases; the runtime ``__subclasses__()``
    walk then resolves indirect subclasses transitively. Over-inclusive (a plain
    ``serializers.Serializer`` also matches), which only means an extra safe
    import — never a missed one.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            # ``ModelSerializer`` (Name) or ``serializers.ModelSerializer``
            # (Attribute) — inspect the trailing identifier either way.
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if "Serializer" in name:
                return True
    return False


def _serializer_defining_modules():
    """
    Dotted names of every project module whose SOURCE defines a serializer, found
    by AST (no import). Excludes ``migrations`` / ``__pycache__`` / ``test*`` /
    ``management`` (migrations & management commands may have import side effects
    and never hold request serializers). This REPLACES the old ``"serializer" in
    path`` heuristic, so a serializer in any module — ``views.py``, a controller,
    etc. — is found.
    """
    modules = set()
    for config in _project_app_configs():
        app_path = Path(config.path)
        for py in app_path.rglob("*.py"):
            rel = py.relative_to(app_path).with_suffix("")
            parts = rel.parts
            if any(p in ("migrations", "__pycache__", "management") for p in parts):
                continue
            if any(p.startswith("test") for p in parts):
                continue
            try:
                source = py.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - unreadable file; nothing to scan
                continue
            if _module_defines_serializer(source):
                modules.add(config.name + "." + ".".join(parts))
    return modules


def import_serializer_modules():
    """
    Import every project module that DEFINES a serializer (found via AST) so its
    ``ModelSerializer`` subclasses exist before ``__subclasses__()`` runs.

    Returns ``(imported, failed)``: ``imported`` the dotted names imported OK, and
    ``failed`` a ``{dotted: repr(error)}`` map. Archival ``SerArc*`` serializers in
    ``models.py`` also come in free via ``django.setup()``. Discovery no longer
    depends on a module's PATH — a serializer hidden in a non-``serializer`` module
    is imported here, or surfaced in ``failed`` and caught by the fail-closed
    meta-test (``KNOWN_UNIMPORTABLE``).
    """
    imported, failed = [], {}
    for dotted in sorted(_serializer_defining_modules()):
        try:
            importlib.import_module(dotted)
            imported.append(dotted)
        except Exception as error:  # noqa: BLE001 - surface via `failed`, fail closed
            logger.error("tenancy discovery: failed to import %s: %s", dotted, error)
            failed[dotted] = repr(error)
    return imported, failed


def _all_modelserializer_subclasses():
    seen, stack = set(), list(ModelSerializer.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


def _in_test_module(cls) -> bool:
    return any(seg.startswith("test") for seg in cls.__module__.split("."))


def _is_project_serializer(cls) -> bool:
    """
    Consider only concrete project serializers: exclude DRF library classes (e.g.
    ``HyperlinkedModelSerializer``), model-less/abstract bases (``Meta.model is
    None``), and serializers defined in test modules (the meta-test's fixtures).
    """
    if cls.__module__.startswith("rest_framework"):
        return False
    meta = getattr(cls, "Meta", None)
    if meta is None or getattr(meta, "model", None) is None:
        return False
    if _in_test_module(cls):
        return False
    return True


def is_writable_relation(field) -> bool:
    return (
        isinstance(field, (RelatedField, ManyRelatedField))
        and not field.read_only
    )


def enumerate_writable_relations(cls):
    """Field names on ``cls`` that are relational AND writable. May raise if the
    serializer cannot be instantiated/bound (callers decide how to handle)."""
    serializer = cls()
    return [name for name, field in serializer.fields.items() if is_writable_relation(field)]


def _related_model(model, field_name, drf_field):
    try:
        return model._meta.get_field(field_name).related_model
    except Exception:  # noqa: BLE001
        qs = getattr(drf_field, "queryset", None)
        if qs is None:
            child = getattr(drf_field, "child_relation", None)  # ManyRelatedField
            qs = getattr(child, "queryset", None)
        return getattr(qs, "model", None)


def discover_all_project_serializers():
    """
    Return ``(records, unintrospectable)`` where ``records`` is a list of dicts
    ``{'key', 'serializer', 'field', 'related_model'}`` — one per writable
    relational field across every project serializer — and ``unintrospectable``
    is the set of ``module.Qualname`` whose ``.fields`` raised.
    """
    import_serializer_modules()
    records = []
    unintrospectable = set()
    for cls in _all_modelserializer_subclasses():
        if not _is_project_serializer(cls):
            continue
        try:
            fields = cls().fields
        except Exception:  # noqa: BLE001 - broken serializer; record, don't hide
            unintrospectable.add(_class_key(cls))
            continue
        model = cls.Meta.model
        for name, field in fields.items():
            if is_writable_relation(field):
                records.append({
                    "key": field_key(cls, name),
                    "serializer": cls,
                    "field": name,
                    "related_model": _related_model(model, name, field),
                })
    return records, unintrospectable


def all_project_serializers():
    """
    Every concrete project ``ModelSerializer`` subclass (imports first). Unlike
    ``discover_all_project_serializers`` it returns the CLASSES themselves —
    including serializers with no writable relation — for the ``fields='__all__'``
    policy check (``all_fields_policy``).
    """
    import_serializer_modules()
    return {
        cls for cls in _all_modelserializer_subclasses()
        if _is_project_serializer(cls)
    }


def read_classifications(cls) -> dict:
    """The ``Meta.tenant_relations`` mapping declared on ``cls`` (or ``{}``)."""
    meta = getattr(cls, "Meta", None)
    return dict(getattr(meta, "tenant_relations", {}) or {})


def _get_field_or_attname(model, name):
    try:
        return model._meta.get_field(name)
    except Exception:  # noqa: BLE001
        if name.endswith("_id"):
            try:
                field = model._meta.get_field(name[:-3])
            except Exception:  # noqa: BLE001
                return None
            if getattr(field, "is_relation", False):
                return field
        return None


def same_tenant_path_resolves(related_model, path: str) -> bool:
    """
    Cheap typo catch: does ``path`` walk cleanly from ``related_model``? Split on
    ``__``, follow each relational segment, accept an FK attname (``x_id``) for
    the final segment. This proves the path is well-formed, NOT that it points at
    the right restaurant — semantic correctness is only proven by the two-tenant
    behavioural tests, and this helper must not claim otherwise.
    """
    if related_model is None or not isinstance(path, str) or not path:
        return False
    model = related_model
    parts = path.split("__")
    for i, part in enumerate(parts):
        field = _get_field_or_attname(model, part)
        if field is None:
            return False
        if i < len(parts) - 1:
            related = getattr(field, "related_model", None)
            if related is None:
                return False
            model = related
    return True
