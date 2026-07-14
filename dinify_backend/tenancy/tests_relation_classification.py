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
suite here is NOT evidence of tenant isolation.
"""
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase
from rest_framework.serializers import ModelSerializer

from restaurants_app.models import MenuItem, MenuSection, Table
from dinify_backend.tenancy.discovery import (
    KNOWN_UNINTROSPECTABLE,
    discover_all_project_serializers,
    enumerate_writable_relations,
    field_key,
    read_classifications,
    same_tenant_path_resolves,
)
from dinify_backend.tenancy.git_ratchet import check_ratchet
from dinify_backend.tenancy.ratchet import (
    BASELINE_PATH,
    classification_violations,
    detect_additions,
    load_baseline,
)
from dinify_backend.tenancy.relations import (
    GlobalRelation,
    SameTenant,
    is_classification,
)


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
