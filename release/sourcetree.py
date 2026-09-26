"""THE SOURCE — which bytes were validated, which bytes are shipped, and proof they are the
same bytes as one exact commit.

WHAT THE CANDIDATE'S SOURCE IS. Every file of the commit's tree, exported by ``git
archive`` straight from git's object store — never by copying the working directory, which
by the time packaging runs holds bytecode, test uploads and audit evidence. Nothing tracked
is left out: the tests, docs and tooling travel with the application, so the archive is
exactly the tree CI checked out. The exclusion rules below are REFUSALS, not filters: if the
tree itself contains an environment file, a database, key material or build output, the
candidate is not built and the path is reported (never the value).

WHAT THE TESTS READ. They run from the checkout, so continuity is checked there: every
tracked path must be a regular file whose bytes hash to the tree's blob and whose
executable bit matches the tree's mode, and every file NOT in the tree must be an approved
product of validation (bytecode beside its tracked source, the audit's evidence, test
uploads) that no Python import can pick up. That is checked before validation (nothing
untracked may exist at all) and again after it. ``git status`` is not the proof: it
honours ``--skip-worktree`` and ``--assume-unchanged``, it compares against the index rather
than the commit, and it is silent about ignored files. Hashing the files is the proof.

WHAT THIS DOES NOT CLAIM. It observes the checkout at two moments and cannot see what a
step did between them and then undid; code running inside CI can alter anything in its
job. The guarantee is against accident, staleness and substitution by the pipeline's own
inputs — not against malicious code executing inside the trusted build.

RECOMPUTING THE TREE. A consumer recomputes the git tree id from the archive's own bytes
(blob, then tree objects, exactly as git hashes them) and compares it with the tree id of
the commit it EXPECTED — taken from its own checkout, not from the candidate — so a
self-consistent replacement archive cannot pass.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import subprocess
import tarfile

GIT_TIMEOUT = 120
PYTHON_IMPORTABLE = (".py", ".pyc", ".pyo", ".pyd", ".so", ".pth")
AFTER_VALIDATION = "after-validation"
BEFORE_VALIDATION = "before-validation"

# Refused outright in the tree (and therefore in any candidate). Path rules first, then
# content rules for key material that has no telltale name.
_ENV_EXAMPLE = ".env.example"
_FORBIDDEN_NAMES = frozenset({"db.sqlite3", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".pypirc", ".netrc"})
_FORBIDDEN_SUFFIXES = (".sqlite3", ".sqlite", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc", ".pyo", ".whl")
_FORBIDDEN_DIRS = frozenset({"__pycache__", "venv", ".venv", "env", "node_modules", "uploads", "media", ".tox", ".mypy_cache"})
_FORBIDDEN_PREFIXES = ("dependency_audit/evidence/",)
_SECRET_CONTENT = (
    ("private key material", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("a cloud access key identifier", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
)


def _problem(code, detail):
    return {"code": code, "detail": detail}


def git(root, *args, binary=False):
    proc = subprocess.run(["git", "-C", root] + list(args), capture_output=True, timeout=GIT_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")[-300:]))
    return proc.stdout if binary else proc.stdout.decode("utf-8")


def blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_id(files):
    """The git tree id of ``{posix path: (mode, blob id hex)}`` — git's own algorithm."""
    root = {}
    for path, (mode, blob) in files.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("%s is both a file and a directory" % path)
        if parts[-1] in node:
            raise ValueError("%s appears twice" % path)
        node[parts[-1]] = (mode, blob)

    def write(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append((name + "/", b"40000 " + name.encode("utf-8") + b"\0" + bytes.fromhex(write(value))))
            else:
                mode, blob = value
                entries.append((name, mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(blob)))
        body = b"".join(e[1] for e in sorted(entries, key=lambda e: e[0].encode("utf-8")))
        return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()

    return write(root)


def forbidden_problems(path, data):
    """Why ``path`` (tracked, posix) must not be in a candidate, or [] — never the value."""
    problems = []
    parts = path.split("/")
    name = parts[-1]
    if (name == ".env" or name.endswith(".env") or (name.startswith(".env.") and path != _ENV_EXAMPLE)
            or name in _FORBIDDEN_NAMES or name.endswith(_FORBIDDEN_SUFFIXES)
            or any(p in _FORBIDDEN_DIRS for p in parts[:-1]) or path.startswith(_FORBIDDEN_PREFIXES)):
        problems.append(_problem("forbidden_path", "%s is an environment file, database, key, dependency or generated "
                                 "output path; it may not be part of a candidate" % path))
    for label, pattern in _SECRET_CONTENT:
        if data is not None and pattern.search(data):
            problems.append(_problem("suspicious_secret", "%s contains what looks like %s; packaging it is refused "
                                     "(the value is not reproduced)" % (path, label)))
    return problems


def _classify_untracked(rel, tracked, phase):
    """An approved product of validation, or None."""
    parts = rel.split("/")
    if phase != AFTER_VALIDATION:
        return None
    if len(parts) >= 2 and parts[-2] == "__pycache__" and parts[-1].endswith(".pyc"):
        stem = parts[-1].split(".")[0]
        source = "/".join(parts[:-2] + [stem + ".py"])
        return "bytecode" if source in tracked else None
    if rel.endswith(PYTHON_IMPORTABLE):
        return None
    if len(parts) == 3 and parts[:2] == ["dependency_audit", "evidence"]:
        return "audit-evidence"
    if parts[0] == "uploads":
        return "test-uploads"
    return None


def observe(root, phase, expected_commit=None, now=None):
    """Compare the working tree with the tree of HEAD, byte for byte. Returns an observation
    dict whose ``problems`` list is empty only when the checkout IS the commit."""
    problems = []
    try:
        commit = git(root, "rev-parse", "HEAD").strip()
        tree = git(root, "rev-parse", "HEAD^{tree}").strip()
        listing = git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD", binary=True)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        return {"phase": phase, "observedAt": now, "problems": [_problem("not_a_checkout", str(error))]}
    if expected_commit and commit != expected_commit:
        problems.append(_problem("wrong_commit", "HEAD is %s, the run is for %s" % (commit, expected_commit)))
    tracked = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, blob = meta.decode("ascii").split(" ")
        path = path.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            problems.append(_problem("unsupported_tracked_entry", "%s is a %s (mode %s); only regular files are supported" % (path, kind, mode)))
            continue
        tracked[path] = (mode, blob)
    for path, (mode, blob) in sorted(tracked.items()):
        full = os.path.join(root, path)
        try:
            st = os.lstat(full)
        except OSError:
            problems.append(_problem("source_changed", "%s is tracked but missing from the checkout" % path))
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append(_problem("source_changed", "%s is not a regular file in the checkout" % path))
            continue
        if bool(st.st_mode & stat.S_IXUSR) != (mode == "100755"):
            problems.append(_problem("source_changed", "%s has the wrong executable bit for mode %s" % (path, mode)))
        with open(full, "rb") as fh:
            data = fh.read()
        if blob_id(data) != blob:
            problems.append(_problem("source_changed", "%s differs from the commit (the file is not the blob HEAD records)" % path))
    untracked, approved = [], {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
        dirnames.sort()
        for filename in sorted(filenames):
            rel = filename if rel_dir == "." else "%s/%s" % (rel_dir.replace(os.sep, "/"), filename)
            if rel in tracked:
                continue
            category = _classify_untracked(rel, tracked, phase)
            if category is None:
                untracked.append(rel)
            else:
                approved[category] = approved.get(category, 0) + 1
    for rel in untracked[:25]:
        problems.append(_problem("unapproved_file", "%s is not in the commit and is not an approved product of validation" % rel))
    if len(untracked) > 25:
        problems.append(_problem("unapproved_file", "... and %d more" % (len(untracked) - 25)))
    return {"phase": phase, "observedAt": now, "commit": commit, "tree": tree, "trackedFiles": len(tracked),
            "treeRecomputed": tree_id(tracked) if tracked else None, "approvedUntracked": approved, "problems": problems}


def export_archive(root, commit, destination):
    """``git archive`` of exactly ``commit`` (not the working tree)."""
    data = git(root, "archive", "--format=tar", commit, binary=True)
    with open(destination, "wb") as fh:
        fh.write(data)
    return len(data)


def read_archive(path, max_member_bytes=64 << 20):
    """Validate a source archive WITHOUT extracting it. Returns ``(files, problems)`` where
    ``files`` is ``{posix path: (mode, bytes)}``.

    Refused: anything but regular files, directories and git's single leading pax global
    header; absolute, empty, ``..``-bearing, backslashed, non-normalised or NUL-bearing
    names; duplicates; a path that is both a file and a directory; oversized members."""
    files, dirs, problems = {}, set(), []
    try:
        with tarfile.open(path, "r:") as tf:
            members = tf.getmembers()
            for index, m in enumerate(members):
                name = m.name
                if m.type == tarfile.XGLTYPE:
                    if index != 0:
                        problems.append(_problem("archive_unsafe", "a pax global header appears after the first member"))
                    continue
                clean = name.rstrip("/") if m.isdir() else name
                if (not clean or clean.startswith("/") or "\\" in clean or "\0" in clean
                        or posixpath.normpath(clean) != clean or clean.split("/")[0] == ".."
                        or ".." in clean.split("/") or clean.startswith("./")):
                    problems.append(_problem("archive_unsafe", "member %r has an unsafe or ambiguous name" % name))
                    continue
                if m.isdir():
                    if clean in files:
                        problems.append(_problem("archive_unsafe", "%s is both a file and a directory" % clean))
                    dirs.add(clean)
                    continue
                if not m.isreg():
                    problems.append(_problem("archive_unsafe", "member %r is a link or special file" % name))
                    continue
                if clean in files:
                    problems.append(_problem("archive_unsafe", "member %r appears more than once" % name))
                    continue
                if m.size > max_member_bytes:
                    problems.append(_problem("archive_unsafe", "member %r is implausibly large" % name))
                    continue
                handle = tf.extractfile(m)
                data = handle.read() if handle else b""
                files[clean] = ("100755" if m.mode & 0o100 else "100644", data)
    except (tarfile.TarError, OSError) as error:
        return {}, [_problem("archive_unreadable", "%s: %s" % (os.path.basename(path), error))]
    for f in files:
        prefix = f + "/"
        if any(d == f or d.startswith(prefix) for d in dirs) or any(g.startswith(prefix) for g in files):
            problems.append(_problem("archive_unsafe", "%s is both a file and a directory" % f))
    for d in dirs:
        if not any(f.startswith(d + "/") for f in files):
            problems.append(_problem("archive_unsafe", "directory %s is empty; git trees carry no empty directories" % d))
    return files, problems


def archive_tree(files):
    return tree_id({p: (mode, blob_id(data)) for p, (mode, data) in files.items()})


def extract(files, destination):
    """Write validated ``files`` under an EMPTY ``destination``. Never follows a link."""
    if os.path.lexists(destination) and os.listdir(destination):
        raise ValueError("%s is not empty" % destination)
    os.makedirs(destination, exist_ok=True)
    base = os.path.realpath(destination)
    for path, (mode, data) in sorted(files.items()):
        full = os.path.join(base, *path.split("/"))
        if not os.path.realpath(os.path.dirname(full)).startswith(base):
            raise ValueError("%s escapes the destination" % path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o755 if mode == "100755" else 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)


def listing_digest(files):
    """A content digest of ``{path: (mode, bytes)}`` independent of archive framing."""
    lines = "".join("%s %s %s\n" % (mode, hashlib.sha256(data).hexdigest(), path) for path, (mode, data) in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()
