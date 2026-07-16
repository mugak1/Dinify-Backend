"""
Meta-test for the tenant-relation classification ratchet (TENANT-STRUCT-00).

Proves every writable relational field on a project ``ModelSerializer`` has been
CONSCIOUSLY classified (``Meta.tenant_relations``) or listed in the temporary
baseline — and proves the guardrail itself catches what it claims, via negative
tests over fixture serializers and REAL git integration tests (a guardrail with
no negative tests is one you trust on faith; we already shipped a test in #219
that would have passed for the wrong reason).

IMPORTANT: green here proves conscious CLASSIFICATION, NOT correctness. A field
can be declared ``SameTenant`` with a validator checking the wrong path; only the
two-tenant behavioural tests (#219/#220/#221) prove tenant isolation. A passing
suite here is NOT evidence of tenant isolation. See
``dinify_backend/tenancy/ASSURANCE.md`` for the precise assurance boundary.
"""
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.serializers import ModelSerializer

from restaurants_app.models import MenuItem, MenuSection, Table
from dinify_backend.tenancy.discovery import (
    KNOWN_UNIMPORTABLE,
    KNOWN_UNINTROSPECTABLE,
    _class_key,
    _serializer_defining_modules,
    all_project_serializers,
    discover_all_project_serializers,
    enumerate_writable_relations,
    field_key,
    import_serializer_modules,
    read_classifications,
    same_tenant_path_resolves,
)
from dinify_backend.tenancy.all_fields_policy import (
    ALL_FIELDS_ALLOWED,
    READ_ARCHIVAL_ALL_FIELDS_ALLOWED,
    all_fields_violations,
    uses_all_fields,
)
from dinify_backend.tenancy.git_ratchet import check_ratchet, resolve_base_ref
from dinify_backend.tenancy.ratchet import (
    BASELINE_PATH,
    classification_violations,
    detect_additions,
    load_baseline,
)
from dinify_backend.tenancy.non_fk_tenant_inventory import validate_inventory
from dinify_backend.tenancy.relations import (
    GlobalRelation,
    SameTenant,
    is_classification,
    resolve_test_ref,
    same_tenant_assurance_violations,
)


# A real, resolvable two-tenant behavioural test used as a valid ``verified_by``.
_REAL_TEST_REF = "restaurants_app.tests.MenuFkTenantBoundaryTests"


# --- fixture serializers (test-only; EXCLUDED from the global discovery because
#     this module's name segment starts with 'test') --------------------------

class _UnclassifiedFixture(ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "section"]  # 'section' is a writable FK, unclassified


class _ParentFixture(ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "section"]


class _ChildFixture(_ParentFixture):
    pass  # no Meta of its own -> inherits 'section'; proves discovery reads .fields


class _AllFieldsFixture(ModelSerializer):
    class Meta:
        model = Table
        fields = "__all__"  # exposes restaurant, dining_area, created_by, deleted_by


class _CorrectlyClassifiedFixture(ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "section", "section_group"]
        tenant_relations = {
            "section": SameTenant("restaurant_id"),
            "section_group": SameTenant("section__restaurant_id"),
        }


class _BadPathFixture(ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "section"]
        tenant_relations = {"section": SameTenant("bogus__path")}


def _classified_keys(cls):
    return {field_key(cls, name) for name in read_classifications(cls)}


def _discovered_keys(cls):
    return {field_key(cls, name) for name in enumerate_writable_relations(cls)}


class TenantRelationMetaTest(SimpleTestCase):
    """The live gate over the real project serializers."""

    def setUp(self):
        self.records, self.unintrospectable = discover_all_project_serializers()
        self.discovered = {r["key"] for r in self.records}
        self.classified = set()
        for cls in {r["serializer"] for r in self.records}:
            self.classified |= _classified_keys(cls)
        self.baselined = load_baseline(BASELINE_PATH)

    def test_every_writable_relation_is_classified_or_baselined(self):
        violations = classification_violations(
            self.discovered, self.classified, self.baselined
        )
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_classifications_have_valid_types_and_resolvable_paths(self):
        related = {(r["serializer"], r["field"]): r["related_model"] for r in self.records}
        problems = []
        for cls in {r["serializer"] for r in self.records}:
            for name, classification in read_classifications(cls).items():
                base = field_key(cls, name)
                if not is_classification(classification):
                    problems.append(f"{base}: not a SameTenant/ServerDerived/GlobalRelation")
                    continue
                if isinstance(classification, SameTenant):
                    model = related.get((cls, name))
                    if not same_tenant_path_resolves(model, classification.path):
                        problems.append(
                            f"{base}: SameTenant path {classification.path!r} does not resolve"
                        )
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_unintrospectable_serializers_match_known(self):
        self.assertEqual(
            self.unintrospectable,
            set(KNOWN_UNINTROSPECTABLE),
            "Un-introspectable serializer set changed. A NEW un-introspectable "
            "serializer must not silently hide its writable relations: fix it, or "
            "add it to discovery.KNOWN_UNINTROSPECTABLE with justification. If you "
            "fixed a listed one, remove it from KNOWN_UNINTROSPECTABLE.",
        )

    def test_all_serializer_modules_import(self):
        # (B) Fail closed: every project module that DEFINES a serializer must
        # import. A module that can't be imported can't be introspected and would
        # silently escape the ratchet.
        _imported, failed = import_serializer_modules()
        self.assertEqual(
            set(failed), set(KNOWN_UNIMPORTABLE),
            "Serializer-defining modules failed to import (or a KNOWN_UNIMPORTABLE "
            f"entry now imports): {failed}. Fix the import, or add it to "
            "discovery.KNOWN_UNIMPORTABLE with justification.",
        )

    def test_no_unapproved_all_fields_serializer(self):
        # (A) Every serializer on fields='__all__' must be an approved exception.
        all_fields_keys = [
            _class_key(cls) for cls in all_project_serializers() if uses_all_fields(cls)
        ]
        violations = all_fields_violations(all_fields_keys, ALL_FIELDS_ALLOWED)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_production_same_tenant_declares_assurance(self):
        # (D) Every PRODUCTION SameTenant classification must link a resolvable
        # two-tenant behavioural test via verified_by. Vacuously green while all
        # relations are baselined (none classified); load-bearing thereafter.
        classified = []
        for cls in {r["serializer"] for r in self.records}:
            for name, classification in read_classifications(cls).items():
                classified.append((field_key(cls, name), classification))
        violations = same_tenant_assurance_violations(classified)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_non_fk_inventory_is_wellformed(self):
        # (E) The non-FK inventory is a well-formed audit list (shape only).
        problems = validate_inventory()
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


class GuardrailNegativeTests(SimpleTestCase):
    """Prove the guardrail catches what it claims (each case must be caught)."""

    def test_unclassified_writable_fk_is_flagged(self):
        discovered = _discovered_keys(_UnclassifiedFixture)
        self.assertIn(field_key(_UnclassifiedFixture, "section"), discovered)
        self.assertTrue(classification_violations(discovered, set(), set()))

    def test_inherited_writable_relation_is_discovered(self):
        # _ChildFixture defines no fields; discovery must read the inherited
        # .fields, not the class's own source.
        self.assertIn("section", enumerate_writable_relations(_ChildFixture))

    def test_new_fields_all_serializer_relations_are_flagged(self):
        discovered = _discovered_keys(_AllFieldsFixture)
        self.assertIn(field_key(_AllFieldsFixture, "restaurant"), discovered)
        self.assertIn(field_key(_AllFieldsFixture, "dining_area"), discovered)
        self.assertTrue(classification_violations(discovered, set(), set()))

    def test_global_relation_requires_nonempty_reason(self):
        with self.assertRaises(ValueError):
            GlobalRelation(reason="")
        with self.assertRaises(ValueError):
            GlobalRelation(reason="   ")

    def test_same_tenant_requires_nonempty_path(self):
        with self.assertRaises(ValueError):
            SameTenant("")

    def test_same_tenant_unresolvable_path_is_rejected(self):
        self.assertFalse(same_tenant_path_resolves(MenuSection, "bogus__path"))
        self.assertTrue(same_tenant_path_resolves(MenuSection, "restaurant_id"))

    def test_bad_path_classification_is_caught(self):
        related = MenuItem._meta.get_field("section").related_model  # MenuSection
        path = read_classifications(_BadPathFixture)["section"].path
        self.assertFalse(same_tenant_path_resolves(related, path))

    def test_detect_additions_flags_an_added_baseline_entry(self):
        self.assertEqual(detect_additions({"A::x"}, {"A::x", "A::y"}), {"A::y"})
        self.assertEqual(detect_additions({"A::x", "A::y"}, {"A::x"}), set())

    def test_correctly_classified_serializer_passes(self):
        discovered = _discovered_keys(_CorrectlyClassifiedFixture)
        classified = _classified_keys(_CorrectlyClassifiedFixture)
        self.assertEqual(discovered, classified)  # every writable relation classified
        self.assertEqual(classification_violations(discovered, classified, set()), [])
        classifications = read_classifications(_CorrectlyClassifiedFixture)
        for name, classification in classifications.items():
            related = MenuItem._meta.get_field(name).related_model
            self.assertTrue(same_tenant_path_resolves(related, classification.path))

    # --- (A) __all__ policy -------------------------------------------------
    def test_all_fields_write_serializer_is_flagged(self):
        # A serializer on fields='__all__' that is NOT an approved exception is
        # flagged. (_AllFieldsFixture is a fixture — not in the allowlist — so it
        # stands in for a NEW write serializer that reached for __all__.)
        self.assertTrue(uses_all_fields(_AllFieldsFixture))
        key = _class_key(_AllFieldsFixture)
        self.assertTrue(all_fields_violations([key], ALL_FIELDS_ALLOWED))

    def test_allowed_read_all_fields_serializer_not_flagged(self):
        # An allow-listed read/archival __all__ serializer must NOT be flagged.
        allowed_key = sorted(READ_ARCHIVAL_ALL_FIELDS_ALLOWED)[0]
        self.assertEqual(all_fields_violations([allowed_key], ALL_FIELDS_ALLOWED), [])

    # --- (B) discovery completeness ----------------------------------------
    def test_serializer_in_unconventional_module_is_discovered(self):
        # AST discovery finds serializers by SOURCE, not module name:
        # restaurants_app.models is NOT a "serializer"-named module yet defines the
        # SerArc* serializers, and must be discovered.
        self.assertIn("restaurants_app.models", _serializer_defining_modules())
        discovered_serializer_modules = {
            cls.__module__ for cls in all_project_serializers()
        }
        self.assertIn("restaurants_app.models", discovered_serializer_modules)

    def test_undiscoverable_serializer_module_fails_closed(self):
        # A serializer-defining module that cannot be imported is collected in the
        # failure map (which the live meta-test asserts must be empty) — it fails
        # closed, never silently vanishing from discovery.
        bogus = "restaurants_app.__nonexistent_serializer_module__"
        with patch(
            "dinify_backend.tenancy.discovery._serializer_defining_modules",
            return_value={bogus},
        ):
            _imported, failed = import_serializer_modules()
        self.assertIn(bogus, failed)

    # --- (D) runtime assurance ---------------------------------------------
    def test_same_tenant_without_assurance_is_flagged(self):
        # A SameTenant with no verified_by is asserting a guarantee nothing proves.
        violations = same_tenant_assurance_violations(
            [("X::section", SameTenant("restaurant_id"))]
        )
        self.assertTrue(violations)
        # An unresolvable verified_by is also flagged.
        self.assertTrue(same_tenant_assurance_violations(
            [("X::section", SameTenant("restaurant_id", verified_by="no.such.Test"))]
        ))

    def test_same_tenant_with_resolvable_assurance_passes(self):
        self.assertTrue(resolve_test_ref(_REAL_TEST_REF))
        violations = same_tenant_assurance_violations(
            [("X::section", SameTenant("restaurant_id", verified_by=_REAL_TEST_REF))]
        )
        self.assertEqual(violations, [])

    # --- (C) push/PR base selection ----------------------------------------
    def test_resolve_base_ref_selects_event_base(self):
        # PR → target branch; push → the pre-push SHA (NOT the tip); all-zeros /
        # nothing → main.
        self.assertEqual(resolve_base_ref({"GITHUB_BASE_REF": "main"}), "main")
        self.assertEqual(resolve_base_ref({"GITHUB_BASE_REF": "develop"}), "develop")
        sha = "a" * 40
        self.assertEqual(resolve_base_ref({"GITHUB_EVENT_BEFORE": sha}), sha)
        self.assertEqual(resolve_base_ref({"GITHUB_EVENT_BEFORE": "0" * 40}), "main")
        self.assertEqual(resolve_base_ref({}), "main")
        # A PR base ref wins over a push before-sha (belt and suspenders).
        self.assertEqual(
            resolve_base_ref({"GITHUB_BASE_REF": "main", "GITHUB_EVENT_BEFORE": sha}),
            "main",
        )


class RatchetGitIntegrationTests(SimpleTestCase):
    """
    Cover the half that actually touches CI — obtaining the base baseline from
    git — not just the pure diff. Unit-testing the easy half and leaving the git
    half uncovered is exactly how a guard passes for the wrong reason.
    """

    def _git(self, repo, *args):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)

    def _init_repo(self, tmp, entries):
        repo = Path(tmp)
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "checkout", "-q", "-b", "main")
        self._write(repo, entries)
        self._git(repo, "add", "baseline.txt")
        self._git(repo, "commit", "-q", "-m", "baseline")
        return repo

    @staticmethod
    def _write(repo, entries):
        (Path(repo) / "baseline.txt").write_text(
            "\n".join(["# baseline header"] + list(entries)) + "\n", encoding="utf-8"
        )

    def test_added_baseline_entry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(tmp, ["A::x", "A::y"])
            self._git(repo, "checkout", "-q", "-b", "feature")
            self._write(repo, ["A::x", "A::y", "A::z"])
            self._git(repo, "commit", "-q", "-am", "add entry")
            code, lines = check_ratchet(repo, "baseline.txt", "main", is_ci=False)
            self.assertEqual(code, 1, "\n".join(lines))
            self.assertTrue(any("A::z" in line for line in lines), "\n".join(lines))

    def test_removed_baseline_entry_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(tmp, ["A::x", "A::y"])
            self._git(repo, "checkout", "-q", "-b", "feature")
            self._write(repo, ["A::x"])  # removed A::y
            self._git(repo, "commit", "-q", "-am", "remove entry")
            code, lines = check_ratchet(repo, "baseline.txt", "main", is_ci=False)
            self.assertEqual(code, 0, "\n".join(lines))

    def test_fail_closed_in_ci_when_base_unresolvable(self):
        # No 'origin' remote + is_ci=True -> the fetch fails -> the guard must FAIL
        # loudly, never pass-with-a-warning in CI.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(tmp, ["A::x"])
            code, lines = check_ratchet(repo, "baseline.txt", "main", is_ci=True)
            self.assertEqual(code, 1, "\n".join(lines))
            self.assertTrue(any("FAIL" in line for line in lines), "\n".join(lines))

    def test_local_run_degrades_when_base_unresolvable(self):
        # Same unresolvable base, but is_ci=False -> warn + pass (local convenience;
        # the meta-test still enforces classify-or-baseline).
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(tmp, ["A::x"])
            code, lines = check_ratchet(repo, "baseline.txt", "nonexistent-ref", is_ci=False)
            self.assertEqual(code, 0, "\n".join(lines))
            self.assertTrue(any("WARN" in line for line in lines), "\n".join(lines))

    def test_direct_push_addition_fails_against_parent(self):
        # The push-to-main bug: an addition committed DIRECTLY on main is invisible
        # when compared against the branch TIP (which already contains it), but the
        # event-aware base (github.event.before ~ the parent SHA) catches it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(tmp, ["A::x", "A::y"])
            parent = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo,
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            # Addition committed straight on main — no feature branch.
            self._write(repo, ["A::x", "A::y", "A::z"])
            self._git(repo, "commit", "-q", "-am", "sneak addition on main")

            # Old behaviour — compare against the branch tip — FALSELY passes:
            # main == HEAD, so the addition is already in the base.
            code, _lines = check_ratchet(repo, "baseline.txt", "main", is_ci=False)
            self.assertEqual(code, 0)

            # Fix — compare against the pre-push parent SHA (what resolve_base_ref
            # returns for a push) — correctly FAILS and names the sneaked entry.
            code, lines = check_ratchet(repo, "baseline.txt", parent, is_ci=False)
            self.assertEqual(code, 1, "\n".join(lines))
            self.assertTrue(any("A::z" in line for line in lines), "\n".join(lines))
