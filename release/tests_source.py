"""THE SOURCE: the checkout the tests read is byte-for-byte the commit, the shipped archive
is exactly the commit's tree, and an archive a consumer receives is refused before
anything in it is written or run. Disposable git repositories and hand-built tar files.
"""

import io
import os
import subprocess
import tarfile
import tempfile
import unittest

from release import sourcetree as st
from release import testing as tt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = {"app/__init__.py": "", "app/views.py": "VALUE = 1\n", "requirements.txt": "six==1.17.0\n", "README.md": "demo\n"}


def codes(observation_or_problems):
    problems = observation_or_problems["problems"] if isinstance(observation_or_problems, dict) else observation_or_problems
    return sorted({p["code"] for p in problems})


class Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "repo")
        self.commit, self.tree = tt.init_repo(self.root, FILES)
        tt.write(self.root, "run.sh", "#!/bin/sh\n", mode=0o755)
        tt.git(self.root, "add", "run.sh")
        tt.git(self.root, "commit", "-qm", "exec")
        self.commit, self.tree = tt.git(self.root, "rev-parse", "HEAD"), tt.git(self.root, "rev-parse", "HEAD^{tree}")

    def tearDown(self):
        self._tmp.cleanup()

    def observe(self, phase=st.BEFORE_VALIDATION, expected=None):
        return st.observe(self.root, phase, expected_commit=expected)


class TheCheckoutIsTheCommit(Repo):
    def test_CONTROL_a_fresh_checkout_is_the_commit_and_its_tree_recomputes(self):
        obs = self.observe(expected=self.commit)
        self.assertEqual(obs["problems"], [])
        self.assertEqual((obs["commit"], obs["tree"], obs["treeRecomputed"]), (self.commit, self.tree, self.tree))

    def test_CONTRACT_the_tree_algorithm_reproduces_git_on_this_repository(self):
        listing = subprocess.run(["git", "-C", ROOT, "ls-tree", "-r", "-z", "--full-tree", "HEAD"], capture_output=True, check=True).stdout
        files = {}
        for rec in listing.split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                mode, _, blob = meta.decode().split(" ")
                files[path.decode()] = (mode, blob)
        want = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD^{tree}"], capture_output=True, check=True, text=True).stdout.strip()
        self.assertEqual(st.tree_id(files), want)

    def test_REGRESSION_a_tracked_change_is_refused(self):
        tt.write(self.root, "app/views.py", "VALUE = 2\n")
        self.assertEqual(codes(self.observe()), ["source_changed"])

    def test_REGRESSION_a_staged_change_is_refused_although_the_index_agrees_with_the_file(self):
        tt.write(self.root, "app/views.py", "VALUE = 3\n")
        tt.git(self.root, "add", "app/views.py")
        self.assertEqual(codes(self.observe()), ["source_changed"])

    def test_REGRESSION_a_skip_worktree_change_is_refused_although_git_status_is_clean(self):
        tt.git(self.root, "update-index", "--skip-worktree", "app/views.py")
        tt.write(self.root, "app/views.py", "VALUE = 'hidden'\n")
        self.assertEqual(tt.git(self.root, "status", "--porcelain"), "", "git status does not see it — the point")
        self.assertEqual(codes(self.observe()), ["source_changed"])

    def test_REGRESSION_an_assume_unchanged_change_is_refused(self):
        tt.git(self.root, "update-index", "--assume-unchanged", "README.md")
        tt.write(self.root, "README.md", "altered\n")
        self.assertEqual(tt.git(self.root, "status", "--porcelain"), "")
        self.assertEqual(codes(self.observe()), ["source_changed"])

    def test_REGRESSION_a_missing_tracked_file_and_a_flipped_exec_bit_are_refused(self):
        os.remove(os.path.join(self.root, "README.md"))
        os.chmod(os.path.join(self.root, "run.sh"), 0o644)
        obs = self.observe()
        self.assertEqual(codes(obs), ["source_changed"])
        self.assertEqual(len(obs["problems"]), 2)

    def test_REGRESSION_the_wrong_commit_is_refused(self):
        self.assertEqual(codes(self.observe(expected="0" * 40)), ["wrong_commit"])

    def test_REGRESSION_an_unapproved_file_is_refused_before_and_after_validation(self):
        tt.write(self.root, "app/injected.py", "raise SystemExit\n")
        self.assertEqual(codes(self.observe()), ["unapproved_file"])
        self.assertEqual(codes(self.observe(st.AFTER_VALIDATION)), ["unapproved_file"])

    def test_REGRESSION_generated_output_is_approved_only_after_validation_and_never_importable(self):
        tt.write(self.root, "app/__pycache__/views.cpython-312.pyc", b"\x00")
        tt.write(self.root, "dependency_audit/evidence/result.json", "{}")
        tt.write(self.root, "uploads/menu/item.png", b"\x89PNG")
        self.assertEqual(codes(self.observe()), ["unapproved_file"], "nothing untracked may exist before validation")
        after = self.observe(st.AFTER_VALIDATION)
        self.assertEqual(after["problems"], [])
        self.assertEqual(after["approvedUntracked"], {"bytecode": 1, "audit-evidence": 1, "test-uploads": 1})
        for path in ("app/__pycache__/ghost.cpython-312.pyc", "uploads/shell.py", "uploads/hook.pth", "dependency_audit/evidence/x.py"):
            tt.write(self.root, path, b"\x00")
            self.assertEqual(codes(self.observe(st.AFTER_VALIDATION)), ["unapproved_file"], path)
            os.remove(os.path.join(self.root, *path.split("/")))

    def test_CONTROL_the_archive_is_the_commit_not_the_working_tree(self):
        tt.write(self.root, "app/views.py", "VALUE = 'dirty'\n")
        out = os.path.join(self._tmp.name, "source.tar")
        st.export_archive(self.root, self.commit, out)
        files, problems = st.read_archive(out)
        self.assertEqual(problems, [])
        self.assertEqual(st.archive_tree(files), self.tree)
        self.assertEqual(files["app/views.py"][1], b"VALUE = 1\n")
        self.assertEqual(files["run.sh"][0], "100755")
        dest = os.path.join(self._tmp.name, "x")
        st.extract(files, dest)
        self.assertTrue(os.access(os.path.join(dest, "run.sh"), os.X_OK))
        with self.assertRaises(ValueError):
            st.extract(files, dest)


def tar_of(members):
    """members: [(name, kind, data)] kind in file/dir/symlink/hardlink/fifo/pax."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size, info.mode = len(data), 0o644
                tf.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE, "pax": tarfile.XGLTYPE}[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = data.decode()
            tf.addfile(info)
    return buf.getvalue()


class AReceivedArchiveIsRefusedBeforeItIsWritten(unittest.TestCase):
    def read(self, members):
        with tempfile.NamedTemporaryFile(suffix=".tar") as fh:
            fh.write(tar_of(members))
            fh.flush()
            return st.read_archive(fh.name)

    def test_CONTROL_plain_files_and_directories_are_read(self):
        files, problems = self.read([("pax_global_header", "pax", b""), ("a", "dir", b""), ("a/b.py", "file", b"x")])
        self.assertEqual((problems, sorted(files)), ([], ["a/b.py"]))

    def test_REGRESSION_poisoned_archives(self):
        cases = {
            "absolute path": [("/etc/cron.d/x", "file", b"x")],
            "parent escape": [("../x.py", "file", b"x")],
            "inner escape": [("a/../../x.py", "file", b"x")],
            "not normalised": [("a//b.py", "file", b"x")],
            "dot prefix": [("./a.py", "file", b"x")],
            "backslash": [("a\\b.py", "file", b"x")],
            "duplicate": [("a.py", "file", b"x"), ("a.py", "file", b"y")],
            "symlink": [("a.py", "symlink", b"/etc/passwd")],
            "hardlink": [("x", "file", b"x"), ("a.py", "hardlink", b"x")],
            "fifo": [("a", "fifo", b"")],
            "late pax header": [("a.py", "file", b"x"), ("pax_global_header", "pax", b"")],
            "file and directory": [("a", "file", b"x"), ("a/b.py", "file", b"y")],
            "empty directory": [("a", "dir", b""), ("b.py", "file", b"x")],
        }
        for label, members in cases.items():
            files, problems = self.read(members)
            # Refused either by our member rules or by tarfile itself — both before any write.
            self.assertTrue(problems, label)
            self.assertTrue(set(codes(problems)) <= {"archive_unsafe", "archive_unreadable"}, (label, problems))

    def test_REGRESSION_an_unreadable_archive_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as fh:
            fh.write(b"not a tar file at all" * 40)
            fh.flush()
            self.assertEqual(codes(st.read_archive(fh.name)[1]), ["archive_unreadable"])


class NothingForbiddenIsPackaged(unittest.TestCase):
    SECRET = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretsecretsecret\n-----END RSA PRIVATE KEY-----\n"

    def test_REGRESSION_forbidden_paths_and_key_material_are_refused_by_path_only(self):
        for path in (".env", "config/.env", "prod.env", ".env.prod", "db.sqlite3", "deploy/server.pem", "certs/tls.key",
                     "uploads/menu.png", "venv/bin/python", "node_modules/x/index.js", "app/__pycache__/v.cpython-312.pyc",
                     "dependency_audit/evidence/result.json", "wheelhouse/six-1.17.0-py2.py3-none-any.whl"):
            self.assertEqual(codes(st.forbidden_problems(path, b"")), ["forbidden_path"], path)
        problems = st.forbidden_problems("app/settings_extra.py", self.SECRET)
        self.assertEqual(codes(problems), ["suspicious_secret"])
        self.assertNotIn("secretsecret", problems[0]["detail"])
        self.assertEqual(codes(st.forbidden_problems("ops/keys.txt", b"id = AKIAABCDEFGHIJKLMNOP\n")), ["suspicious_secret"])

    def test_CONTROL_the_documented_template_and_ordinary_source_pass(self):
        for path in (".env.example", "app/env_utils.py", "dinify_backend/settings.py", "restaurants_app/media_paths.py"):
            self.assertEqual(st.forbidden_problems(path, b"x = 1\n"), [], path)

    def test_CONTRACT_the_committed_tree_contains_nothing_forbidden(self):
        listing = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True, check=True).stdout
        found = []
        for path in listing.decode().split("\0"):
            if path:
                with open(os.path.join(ROOT, path), "rb") as fh:
                    found += st.forbidden_problems(path, fh.read())
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
