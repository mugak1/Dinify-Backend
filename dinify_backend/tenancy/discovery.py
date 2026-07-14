"""
Discovery of writable relational fields across every project ``ModelSerializer``
(TENANT-STRUCT-00).

A serializer built with ``fields='__all__'`` (or an explicit list that includes a
relation without ``read_only``) exposes that FK/O2O/M2M as WRITABLE from the
request body. This module enumerates every such (serializer, field) pair by
INTROSPECTING the instantiated serializer's ``.fields`` — not by scanning source
text — so a relation inherited from a parent serializer is still found. The
tenancy meta-test and the baseline generator both build on this.

Imported ONLY by tooling/tests; never by a request path.
"""
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
#
# restaurants_app.serializers.SerializerEmployeeGetRestaurant: already broken —
#   Meta.fields names a 'name' field that RestaurantEmployee does not have, so
#   `.fields` raises ImproperlyConfigured unconditionally (dead code; it would
#   500 on any request). Fixing it is a separate change, not this PR.
KNOWN_UNINTROSPECTABLE = frozenset({
    "restaurants_app.serializers.SerializerEmployeeGetRestaurant",
})


def _class_key(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def field_key(cls, field_name: str) -> str:
    """Stable key for a (serializer, field) pair, used by the baseline file."""
    return f"{_class_key(cls)}::{field_name}"


def import_all_serializer_modules():
    """
    Import every serializer-bearing module in the PROJECT apps so their
    ``ModelSerializer`` subclasses are defined before ``__subclasses__()`` runs.

    ``__subclasses__()`` only sees classes already imported. Archival ``SerArc*``
    serializers live in ``models.py`` (loaded by ``django.setup()``); the rest
    live in unevenly-named modules (``serializers.py``, ``serializers_kitchen.py``,
    a ``serializers/`` package). We therefore import any ``.py`` whose path
    contains ``serializer`` under each project app. Import failures are logged
    loudly — a serializer module that won't import is a real problem.
    """
    imported = []
    for config in apps.get_app_configs():
        app_path = Path(config.path)
        if "site-packages" in str(app_path):
            continue  # third-party / django.contrib app
        for py in app_path.rglob("*.py"):
            rel = py.relative_to(app_path).with_suffix("")
            parts = rel.parts
            if any(p in ("migrations", "__pycache__") for p in parts):
                continue
            if any(p.startswith("test") for p in parts):
                continue
            if not any("serializer" in p.lower() for p in parts):
                continue
            dotted = config.name + "." + ".".join(parts)
            try:
                importlib.import_module(dotted)
                imported.append(dotted)
            except Exception as error:  # noqa: BLE001 - surface, don't swallow
                logger.error("tenancy discovery: failed to import %s: %s", dotted, error)
    return imported


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
    import_all_serializer_modules()
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
