"""
Git-backed baseline-addition check (TENANT-STRUCT-00).

Detecting that a baseline entry was *added* (vs one that was always there)
fundamentally needs a prior-state reference — a current-tree check cannot tell
them apart. This module reads the baseline as it exists on the base branch and
compares. It is deliberately separate from ``ratchet.py`` (which stays pure/
git-free) so the git-touching half is covered by real git integration tests, and
so ``scripts/check_tenant_relation_ratchet.py`` can stay a thin CLI wrapper.

Imported only by that script and the tests.
"""
import os
import subprocess
from pathlib import Path

from dinify_backend.tenancy.ratchet import detect_additions


def _run_git(repo_dir, args):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )


def in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or bool(os.environ.get("CI"))


def _parse_keys(text):
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _resolve_base_commit(repo_dir, base_ref, is_ci):
    """A git committish for the base branch, or None if it can't be resolved.

    In CI we fetch the base tip (the checkout token allows it); locally we try
    existing refs first — so the integration tests' temp repo resolves a plain
    local branch with no remote — then fall back to a fetch.
    """
    if is_ci:
        if _run_git(repo_dir, ["fetch", "--depth=1", "origin", base_ref]).returncode == 0:
            return "FETCH_HEAD"
        return None
    for candidate in (f"origin/{base_ref}", base_ref):
        if _run_git(repo_dir, ["rev-parse", "--verify", "--quiet", candidate]).returncode == 0:
            return candidate
    if _run_git(repo_dir, ["fetch", "--depth=1", "origin", base_ref]).returncode == 0:
        return "FETCH_HEAD"
    return None


def _baseline_at(repo_dir, commit, baseline_rel):
    """Baseline key-set at ``commit``, or None if the file does not exist there."""
    if _run_git(repo_dir, ["cat-file", "-e", f"{commit}:{baseline_rel}"]).returncode != 0:
        return None
    show = _run_git(repo_dir, ["show", f"{commit}:{baseline_rel}"])
    if show.returncode != 0:
        return None
    return _parse_keys(show.stdout)


def check_ratchet(repo_dir, baseline_rel, base_ref, is_ci):
    """
    Compare ``repo_dir/baseline_rel`` against the same file on the base branch.
    Returns ``(exit_code, lines)``. Fully drivable from a temp repo, which the
    git integration tests exploit.

    Fail-closed in CI: if the base can't be read and ``is_ci`` is True → exit 1.
    Otherwise (local run) → warn + exit 0. Bootstrap (baseline absent on base) →
    exit 0.
    """
    lines = []
    current = _parse_keys(Path(repo_dir, baseline_rel).read_text(encoding="utf-8"))

    commit = _resolve_base_commit(repo_dir, base_ref, is_ci)
    if commit is None:
        detail = f"could not resolve base ref 'origin/{base_ref}'"
        if is_ci:
            lines.append(
                f"tenant-relation ratchet: FAIL — {detail}. Refusing to pass: a "
                f"ratchet that cannot read the base protects nothing."
            )
            return 1, lines
        lines.append(
            f"tenant-relation ratchet: WARN — {detail}; skipping the addition check "
            f"(local run). The meta-test still enforces classify-or-baseline. "
            f"Current baseline: {len(current)} entries."
        )
        return 0, lines

    base = _baseline_at(repo_dir, commit, baseline_rel)
    if base is None:
        lines.append(
            f"tenant-relation ratchet: baseline is new on 'origin/{base_ref}' "
            f"(bootstrap) — nothing to compare. Current baseline: {len(current)} entries."
        )
        return 0, lines

    added = detect_additions(base, current)
    if added:
        lines.append(
            "tenant-relation ratchet: FAIL — the baseline GREW. Entries may only be "
            "REMOVED, never added; a new writable relation must be CLASSIFIED in "
            "Meta.tenant_relations, not baselined:"
        )
        lines.extend(f"  + {key}" for key in sorted(added))
        return 1, lines

    removed = len(base) - len(current)
    tail = f" ({removed} removed since base)." if removed > 0 else "."
    lines.append(
        f"tenant-relation ratchet: OK — no baseline additions. "
        f"{len(current)} entries remaining{tail}"
    )
    return 0, lines
