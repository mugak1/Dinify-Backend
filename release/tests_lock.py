"""THE LOCK: the committed lock is the reviewed resolution of the committed direct inputs,
and every way the two could drift apart — or the lock could admit something the target
cannot run — is refused by name. Offline; each case changes ONE fact.
"""

import copy
import json
import os
import unittest

from release import lockfile as lf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(ROOT, lf.LOCK_PATH), "rb") as _fh:
    LOCK_BYTES = _fh.read()
with open(os.path.join(ROOT, lf.REQUIREMENTS_PATH), "rb") as _fh:
    REQUIREMENTS = _fh.read()
LOCK = json.loads(LOCK_BYTES.decode("utf-8"))


def codes(problems):
    return sorted({p["code"] for p in problems})


def check(lock, requirements=REQUIREMENTS):
    return lf.check(lf.lock_bytes(lock), requirements)[1]


def mutated(fn):
    lock = copy.deepcopy(LOCK)
    fn(lock)
    return lock


def package(lock, name):
    return next(e for e in lock["packages"] if e["name"] == name)


class TheCommittedLock(unittest.TestCase):
    def test_CONTRACT_the_committed_lock_is_the_resolution_of_the_committed_requirements(self):
        lock, problems = lf.check(LOCK_BYTES, REQUIREMENTS)
        self.assertEqual(problems, [])
        pins, _ = lf.parse_direct_inputs(REQUIREMENTS.decode("utf-8"))
        direct = {e["name"]: e["version"] for e in lock["packages"] if e["direct"]}
        self.assertEqual(direct, pins)

    def test_CONTRACT_the_lock_is_in_its_one_canonical_serialisation(self):
        self.assertEqual(lf.lock_bytes(json.loads(LOCK_BYTES.decode("utf-8"))), LOCK_BYTES)

    def test_CONTRACT_installer_application_and_generator_are_labelled_apart(self):
        self.assertEqual([e["name"] for e in LOCK["bootstrap"]], ["pip"])
        self.assertNotIn("pip", [e["name"] for e in LOCK["packages"]])
        self.assertEqual((LOCK["generator"]["tool"], LOCK["generator"]["version"], LOCK["generator"]["wheelSha256"]),
                         ("pip", LOCK["bootstrap"][0]["version"], LOCK["bootstrap"][0]["sha256"]))

    def test_CONTRACT_the_only_undeclared_packages_are_required_transitives(self):
        undeclared = {e["name"]: e["requiredBy"] for e in LOCK["packages"] if not e["direct"]}
        self.assertEqual(undeclared, {"cffi": ["cryptography"], "pycparser": ["cffi"]})

    def test_CONTRACT_every_locked_file_is_a_wheel_the_declared_target_supports(self):
        for e in lf.entries(LOCK):
            self.assertTrue(lf.wheel_supported(e["filename"], LOCK["target"]), e["filename"])
        self.assertEqual((LOCK["target"]["python"], LOCK["target"]["machine"]), ("3.12.3", "x86_64"))

    def test_CONTRACT_the_lock_target_is_the_ci_interpreter(self):
        with open(os.path.join(ROOT, "dependency_audit", "policy.json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["target"]["python"], LOCK["target"]["python"])


class DirectInputsAndTheLockCannotDriftApart(unittest.TestCase):
    def test_REGRESSION_a_changed_requirements_file_with_the_old_lock_is_refused(self):
        bumped = REQUIREMENTS.replace(b"sqlparse==0.6.0", b"sqlparse==0.6.1")
        self.assertIn("stale_lock", codes(check(LOCK, bumped)))
        self.assertIn("direct_version_mismatch", codes(check(LOCK, bumped)))

    def test_REGRESSION_even_a_comment_edit_is_a_stale_lock(self):
        self.assertEqual(codes(check(LOCK, REQUIREMENTS + b"# a note\n")), ["stale_lock"])

    def test_REGRESSION_a_new_direct_requirement_without_a_locked_file_is_refused(self):
        self.assertIn("missing_direct", codes(check(LOCK, REQUIREMENTS + b"colorama==0.4.6\n")))

    def test_REGRESSION_a_removed_direct_requirement_leaves_a_stale_direct_flag(self):
        removed = REQUIREMENTS.replace(b"qrcode==8.2\n", b"")
        self.assertEqual(codes(check(LOCK, removed)), ["direct_flag_mismatch", "stale_lock"])

    def test_REGRESSION_unsupported_direct_input_forms_are_refused_not_dropped(self):
        for line in ("django", "django>=5.2", 'x==1; sys_platform == "win32"', "psycopg[binary]==3.1.18",
                     "demo @ https://example.invalid/demo.whl", "-r other.txt", "--hash=sha256:00", "six==1.17.0  # pinned"):
            pins, problems = lf.parse_direct_inputs("asgiref==3.11.1\n%s\n" % line)
            self.assertEqual(codes(problems), ["unsupported_direct_input"], line)
            self.assertEqual(pins, {"asgiref": "3.11.1"}, line)

    def test_REGRESSION_a_duplicate_direct_input_is_refused_after_normalisation(self):
        _, problems = lf.parse_direct_inputs("Django==5.2.17\ndjango==5.2.17\n")
        self.assertEqual(codes(problems), ["duplicate_direct_input"])
        _, problems = lf.parse_direct_inputs("typing_extensions==4.13.2\ntyping-extensions==4.13.2\n")
        self.assertEqual(codes(problems), ["duplicate_direct_input"])

    def test_CONTROL_comments_and_blank_lines_carry_no_requirement(self):
        pins, problems = lf.parse_direct_inputs("# heading\n\nasgiref==3.11.1\n   # backports.zoneinfo==0.2.1\n")
        self.assertEqual((pins, problems), ({"asgiref": "3.11.1"}, []))


class TheLockAdmitsOnlyWhatTheTargetRuns(unittest.TestCase):
    def test_REGRESSION_an_incompatible_wheel_is_refused(self):
        for filename in ("cryptography-50.0.0-cp311-abi3-win_amd64.whl", "cryptography-50.0.0-cp311-abi3-macosx_10_9_universal2.whl",
                         "cryptography-50.0.0-cp311-abi3-manylinux_2_40_x86_64.whl", "cryptography-50.0.0-cp311-abi3-manylinux_2_34_aarch64.whl",
                         "cryptography-50.0.0-cp313-abi3-manylinux_2_34_x86_64.whl", "cryptography-50.0.0-cp311-cp311-manylinux_2_34_x86_64.whl",
                         "cryptography-50.0.0-cp311-abi3-musllinux_1_2_x86_64.whl"):
            def swap(lock, f=filename):
                e = package(lock, "cryptography")
                e["filename"] = f
                e["url"] = e["url"].rsplit("/", 1)[0] + "/" + f
            self.assertEqual(codes(check(mutated(swap))), ["wheel_incompatible"], filename)

    def test_REGRESSION_a_source_distribution_is_never_locked(self):
        def sdist(lock):
            e = package(lock, "sqlparse")
            e["filename"] = "sqlparse-0.6.0.tar.gz"
            e["url"] = e["url"].rsplit("/", 1)[0] + "/sqlparse-0.6.0.tar.gz"
        self.assertIn("lock_invalid", codes(check(mutated(sdist))))

    def test_REGRESSION_a_package_the_target_does_not_need_is_refused(self):
        def extra(lock):
            lock["packages"].append({"name": "zzz-win-only", "version": "1.0", "filename": "zzz_win_only-1.0-py3-none-any.whl",
                                     "url": lf.FILE_HOST + "aa/zzz_win_only-1.0-py3-none-any.whl", "sha256": "0" * 64, "size": 1,
                                     "direct": False, "requiredBy": []})
        self.assertEqual(codes(check(mutated(extra))), ["unexpected_package"])

    def test_REGRESSION_a_transitive_whose_parent_is_not_locked_is_refused(self):
        self.assertIn("lock_invalid", codes(check(mutated(lambda lock: lock["packages"].remove(package(lock, "cffi"))))))

    def test_REGRESSION_structural_single_rule_mutations(self):
        cases = {
            "duplicate name": lambda l: l["packages"].insert(1, dict(package(l, "asgiref"))),
            "unnormalized name": lambda l: package(l, "pyjwt").update(name="PyJWT"),
            "filename names another package": lambda l: package(l, "six").update(version="1.16.0"),
            "url is not the file host": lambda l: package(l, "six").update(url="https://example.invalid/packages/" + package(l, "six")["filename"]),
            "url names another file": lambda l: package(l, "six").update(url=lf.FILE_HOST + "aa/six-1.16.0-py2.py3-none-any.whl"),
            "url carries a query": lambda l: package(l, "six").update(url=package(l, "six")["url"] + "?x=1"),
            "short hash": lambda l: package(l, "six").update(sha256="abc"),
            "uppercase hash": lambda l: package(l, "six").update(sha256=package(l, "six")["sha256"].upper()),
            "boolean size": lambda l: package(l, "six").update(size=True),
            "zero size": lambda l: package(l, "six").update(size=0),
            "unknown field": lambda l: package(l, "six").update(extra="x"),
            "missing field": lambda l: package(l, "six").pop("url"),
            "unsorted": lambda l: l["packages"].reverse(),
            "bootstrap is not pip": lambda l: l["bootstrap"][0].update(name="setuptools"),
            "two installers": lambda l: l["bootstrap"].append(dict(l["bootstrap"][0])),
            "generator is not the installer": lambda l: l["generator"].update(version="24.0"),
            "another index": lambda l: l["generator"].update(index="https://test.pypi.org/simple/"),
            "markers contradict the target": lambda l: l["target"]["markers"].update(python_full_version="3.12.4"),
            "a kernel-specific marker": lambda l: l["target"]["markers"].update(platform_release="6.8"),
            "unknown top-level field": lambda l: l.update(note="x"),
            "another repository": lambda l: l.update(repository="mugak1/Dinify-Admin"),
            "another schema": lambda l: l.update(schema="dinify.backend.python-lock/2"),
            "requiredBy not sorted": lambda l: package(l, "django").update(requiredBy=["djangorestframework", "django-cors-headers"]),
        }
        for label, fn in cases.items():
            problems = check(mutated(fn))
            self.assertTrue(problems, label)
            self.assertTrue(set(codes(problems)) <= {"lock_invalid", "duplicate_package", "wheel_incompatible"}, (label, problems))

    def test_CONTROL_the_static_tag_rule(self):
        target = LOCK["target"]
        yes = [("py3", "none", "any"), ("py312", "none", "any"), ("cp312", "cp312", "manylinux_2_17_x86_64"),
               ("cp311", "abi3", "manylinux_2_34_x86_64"), ("cp312", "cp312", "manylinux2014_x86_64"), ("cp39", "abi3", "manylinux1_x86_64")]
        no = [("cp312", "cp312", "manylinux_2_40_x86_64"), ("cp313", "cp313", "manylinux_2_17_x86_64"), ("cp312", "cp312", "win_amd64"),
              ("pp310", "pypy310_pp73", "manylinux_2_17_x86_64"), ("cp312", "cp312", "linux_x86_64"), ("py3", "cp312", "any"),
              ("py2", "none", "any")]
        for tag in yes:
            self.assertTrue(lf.tag_supported(tag, target), tag)
        for tag in no:
            self.assertFalse(lf.tag_supported(tag, target), tag)
        # A compressed tag set is supported when ANY of its expansions is: py2.py3 through py3.
        self.assertTrue(lf.wheel_supported("six-1.17.0-py2.py3-none-any.whl", target))
        self.assertEqual(lf.required_glibc(package(LOCK, "cryptography")["filename"]), "2.34")
        self.assertIsNone(lf.required_glibc(package(LOCK, "django")["filename"]))


if __name__ == "__main__":
    unittest.main()
