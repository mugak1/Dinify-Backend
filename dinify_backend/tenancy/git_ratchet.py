"""
Git-backed baseline-addition check (TENANT-STRUCT-00).

Detecting that a baseline entry was *added* (vs one that was always there)
fundamentally needs a prior-state reference — a current-tree check cannot tell
them apart. This module reads the baseline as it exists at the comparison BASE and
compares. It is deliberately separate from ``ratchet.py`` (which stays pure/
git-free) so the git-touching half is covered by real git integration tests, and
so ``scripts/check_tenant_relation_ratchet.py`` can stay a thin CLI wrapper.

Choosing the base (``resolve_base_ref``) is event-aware:
  * pull_request → the PR's target branch (``GITHUB_BASE_REF``); the PR's additions
    are not yet on it, so they are detected;
  * push → the commit BEFORE the push (``github.event.before`` via
    ``GITHUB_EVENT_BEFORE``), NOT the branch tip — on a push the tip already
    includes the new commit, so comparing against it would make an addition
    invisible (the historical push-to-main false-pass this module fixes);
  * neither (local / manual) → the default branch ``main``.

Branch-protection note: when ``main`` requires PRs (no direct pushes), the
pull_request comparison is the authoritative gate and the push comparison is
defense-in-depth for direct pushes.

WHAT COUNTS AS A COMPARISON (D08 B2.3). The check is split into ``observe`` (git)
and ``decide`` (pure), and ``decide`` is explicit about every state in which NO
comparison happened, because a green step that compared nothing is the failure
this ratchet exists to prevent:

  * the working-tree baseline is MISSING → incomplete everywhere. Zero debt is an
    empty baseline file (a real, passing comparison); a missing file is missing
    evidence, and the documented end state deletes this check with it;
  * the event names no usable base (a push with no ``before``, a pull request with
    no ``GITHUB_BASE_REF``) → CI refuses; it used to fall back to ``main``, which on
    a push IS the pushed commit;
  * the base resolves to the very commit under test → CI refuses: a baseline
    compared with itself has no additions by construction;
  * the base cannot be read, or its baseline path is not a readable file → CI
    refuses (unchanged in intent; now also covers an unreadable blob, which used to
    be indistinguishable from an absent one);
  * the baseline is absent at the base → BOOTSTRAP only if the base also predates the
    ratchet itself; if the base already had the ratchet, the baseline was moved or
    removed, and "nothing to compare" would let a moved baseline carry additions →
    CI refuses.

Locally every refusal above except the missing working-tree file degrades to a
labelled ``NO COMPARISON PERFORMED`` exit 0 — the documented local convenience — and
the label is part of the output so it cannot be read as a pass that compared.

Exit codes: 0 compared-and-clean (or a labelled bootstrap / local skip), 1 additions,
2 incomplete (never clean). The CLI adds 3 for a failed self-test.

Imported only by that script and the tests.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dinify_backend.tenancy.ratchet import detect_additions

EXIT_CLEAN, EXIT_ADDITIONS, EXIT_INCOMPLETE = 0, 1, 2

# The ratchet's own CLI. Its presence at the comparison base is what distinguishes a
# genuine bootstrap (the base predates the ratchet) from a moved or deleted baseline.
RATCHET_CLI_REL = "scripts/check_tenant_relation_ratchet.py"

NO_COMPARISON = "NO COMPARISON PERFORMED"


def _run_git(repo_dir, args):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
    )


def in_ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or bool(os.environ.get("CI"))


def resolve_base_ref(env):
    """
    The git ref/commit to compare the baseline against, chosen from the CI ``env``
    mapping (event-aware — see the module docstring):

    * ``GITHUB_BASE_REF`` set (pull_request) → that target branch;
    * else ``GITHUB_EVENT_BEFORE`` set and not all-zeros (push) → that pre-push
      commit SHA;
    * else, when ``GITHUB_EVENT_NAME`` says the event was a push or a pull request →
      ``None``: that event HAS a base and this one could not be identified, and
      falling back to ``main`` would compare a pushed commit with itself;
    * else (local / manual, or an all-zeros ``before`` with no event named) →
      ``"main"``.

    Returning a raw SHA is fine: ``_resolve_base_commit`` fetches/verifies any
    committish, and the baseline is read at ``<commit>:<path>`` unchanged.
    """
    base_ref = (env.get("GITHUB_BASE_REF") or "").strip()
    if base_ref:
        return base_ref
    before = (env.get("GITHUB_EVENT_BEFORE") or "").strip()
    if before and set(before) != {"0"}:  # not the all-zeros "no previous commit"
        return before
    event = (env.get("GITHUB_EVENT_NAME") or "").strip()
    if event in ("push", "pull_request", "pull_request_target"):
        return None
    return "main"


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


def _commit_sha(repo_dir, committish):
    r = _run_git(repo_dir, ["rev-parse", "--verify", "--quiet", f"{committish}^{{commit}}"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _path_at(repo_dir, commit, rel):
    """``('absent', None)``, ``('blob', <text>)`` or ``('unreadable', <why>)``."""
    listing = _run_git(repo_dir, ["ls-tree", "-z", commit, "--", rel])
    if listing.returncode != 0:
        return "unreadable", (listing.stderr or "git ls-tree failed").strip()
    if not listing.stdout.strip("\0"):
        return "absent", None
    meta = listing.stdout.split("\t", 1)[0].split()
    if len(meta) != 3 or meta[1] != "blob":
        return "unreadable", f"{rel} is a {meta[1] if len(meta) > 1 else '?'} at the base, not a file"
    blob = _run_git(repo_dir, ["cat-file", "blob", meta[2]])
    if blob.returncode != 0:
        return "unreadable", (blob.stderr or "git cat-file failed").strip()
    return "blob", blob.stdout


@dataclass
class Observation:
    """What git and the working tree said, before anything is decided."""
    base_ref: object              # str, or None when the event named no usable base
    current: object = None        # set of keys, or None when the file is missing/unreadable
    current_error: str = ""
    base: str = "unresolved"      # unresolved | self | unreadable | bootstrap | moved | present
    base_keys: object = None
    base_sha: str = ""
    detail: str = ""


def observe(repo_dir, baseline_rel, base_ref, is_ci, ratchet_rel=RATCHET_CLI_REL):
    obs = Observation(base_ref=base_ref)
    try:
        obs.current = _parse_keys(Path(repo_dir, baseline_rel).read_text(encoding="utf-8"))
    except OSError as exc:
        obs.current_error = f"{baseline_rel} could not be read: {exc.strerror or exc}"
        return obs
    if base_ref is None:
        obs.detail = "the event names no comparison base (a push with no pre-push commit, or a pull request with no target)"
        return obs

    commit = _resolve_base_commit(repo_dir, base_ref, is_ci)
    if commit is None:
        obs.detail = f"could not resolve comparison base '{base_ref}'"
        return obs
    obs.base_sha = _commit_sha(repo_dir, commit) or ""
    head_sha = _commit_sha(repo_dir, "HEAD")
    if obs.base_sha and head_sha and obs.base_sha == head_sha:
        obs.base = "self"
        obs.detail = (f"comparison base '{base_ref}' is the commit under test "
                      f"({obs.base_sha[:12]}) — a baseline compared with itself has no additions by construction")
        return obs

    state, value = _path_at(repo_dir, commit, baseline_rel)
    if state == "unreadable":
        obs.base = "unreadable"
        obs.detail = f"the baseline at base '{base_ref}' could not be read: {value}"
    elif state == "absent":
        ratchet_state, _ = _path_at(repo_dir, commit, ratchet_rel)
        if ratchet_state == "absent":
            obs.base = "bootstrap"
            obs.detail = f"base '{base_ref}' predates the ratchet ({ratchet_rel} and {baseline_rel} are both absent there)"
        else:
            obs.base = "moved"
            obs.detail = (f"base '{base_ref}' already has the ratchet but no {baseline_rel} — the baseline was "
                          f"moved or removed, and a moved baseline cannot be compared; keep it at its path")
    else:
        obs.base = "present"
        obs.base_keys = _parse_keys(value)
    return obs


def decide(obs, is_ci):
    """``(exit_code, lines)`` for an ``Observation``. Pure — the self-test drives it."""
    if obs.current is None:
        return EXIT_INCOMPLETE, [
            f"tenant-relation ratchet: INCOMPLETE — {obs.current_error}. Missing evidence is not zero "
            "debt: an EMPTY baseline is zero debt. When the debt reaches zero, delete this check with "
            "the file (TENANT-STRUCT-00), do not leave a gate that reads nothing."
        ]
    current = obs.current
    count = f"Current baseline: {len(current)} entries."

    if obs.base == "present":
        added = detect_additions(obs.base_keys, current)
        if added:
            return EXIT_ADDITIONS, [
                "tenant-relation ratchet: FAIL — the baseline GREW. Entries may only be "
                "REMOVED, never added; a new writable relation must be CLASSIFIED in "
                "Meta.tenant_relations, not baselined:",
                *(f"  + {key}" for key in sorted(added)),
            ]
        removed = len(obs.base_keys) - len(current)
        tail = f" ({removed} removed since base)" if removed > 0 else ""
        zero = (" The debt is ZERO: per TENANT-STRUCT-00, delete the baseline, the ratchet script "
                "and its ci.yml/verify.sh steps." if not current else "")
        sha = f" ({obs.base_sha[:12]})" if obs.base_sha else ""
        return EXIT_CLEAN, [
            f"tenant-relation ratchet: OK — COMPARED against base '{obs.base_ref}'{sha}; no "
            f"baseline additions. {len(current)} entries remaining{tail}.{zero}"
        ]

    if obs.base == "bootstrap":
        return EXIT_CLEAN, [
            f"tenant-relation ratchet: BOOTSTRAP — {NO_COMPARISON}: {obs.detail}. {count}"
        ]

    # unresolved / self / unreadable / moved: nothing was compared.
    if is_ci:
        return EXIT_INCOMPLETE, [
            f"tenant-relation ratchet: FAIL — {obs.detail}. Refusing to pass: a ratchet that "
            f"cannot compare against its base protects nothing ({NO_COMPARISON})."
        ]
    return EXIT_CLEAN, [
        f"tenant-relation ratchet: WARN — {NO_COMPARISON} (local run): {obs.detail}; skipping "
        f"the addition check. The meta-test still enforces classify-or-baseline. {count}"
    ]


def check_ratchet(repo_dir, baseline_rel, base_ref, is_ci, ratchet_rel=RATCHET_CLI_REL):
    """
    Compare ``repo_dir/baseline_rel`` against the same file on the base branch.
    Returns ``(exit_code, lines)``. Fully drivable from a temp repo, which the
    git integration tests exploit.

    Fail-closed in CI: anything that means no comparison happened → exit 2.
    Locally → a labelled warning + exit 0. A genuine bootstrap (the base predates
    the ratchet) → a labelled exit 0.
    """
    return decide(observe(repo_dir, baseline_rel, base_ref, is_ci, ratchet_rel), is_ci)


# ---------------------------------------------------------------------------
# Self-test: pure, run by the CLI before every check.
# ---------------------------------------------------------------------------

def self_test(log=print) -> int:
    failures = []

    def expect(label, condition):
        if not condition:
            failures.append(label)

    expect("an added entry is detected", detect_additions({"A::x"}, {"A::x", "A::y"}) == {"A::y"})
    expect("a removed entry is not an addition", detect_additions({"A::x", "A::y"}, {"A::x"}) == set())
    expect("an unchanged baseline has no additions", detect_additions({"A::x"}, {"A::x"}) == set())
    expect("comments and blank lines are not entries", _parse_keys("# h\n\n  A::x  \n#B::y\n") == {"A::x"})

    sha = "a" * 40
    for env, want in (
        ({"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"}, "main"),
        ({"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_BEFORE": sha}, sha),
        ({"GITHUB_EVENT_NAME": "push"}, None),
        ({"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_BEFORE": "0" * 40}, None),
        ({"GITHUB_EVENT_NAME": "pull_request"}, None),
        ({}, "main"),
    ):
        expect(f"base selection for {env}", resolve_base_ref(env) == want)

    present = Observation(base_ref="main", current={"A::x", "A::y"}, base="present", base_keys={"A::x"})
    expect("CI: an addition fails", decide(present, True)[0] == EXIT_ADDITIONS)
    expect("locally: an addition fails too", decide(present, False)[0] == EXIT_ADDITIONS)
    shrunk = Observation(base_ref="main", current={"A::x"}, base="present", base_keys={"A::x", "A::y"})
    expect("a shrink passes, and says it compared",
           decide(shrunk, True)[0] == EXIT_CLEAN and "COMPARED" in decide(shrunk, True)[1][0])
    zero = Observation(base_ref="main", current=set(), base="present", base_keys={"A::x"})
    expect("zero debt passes and says so", decide(zero, True)[0] == EXIT_CLEAN and "ZERO" in decide(zero, True)[1][0])
    missing = Observation(base_ref="main", current=None, current_error="gone")
    expect("a missing baseline is incomplete, even locally",
           decide(missing, True)[0] == EXIT_INCOMPLETE and decide(missing, False)[0] == EXIT_INCOMPLETE)
    for state in ("unresolved", "self", "unreadable", "moved"):
        obs = Observation(base_ref="main", current={"A::x"}, base=state, detail=state)
        code_ci, lines_ci = decide(obs, True)
        code_local, lines_local = decide(obs, False)
        expect(f"CI refuses a '{state}' base", code_ci == EXIT_INCOMPLETE)
        expect(f"a local '{state}' base is a LABELLED skip",
               code_local == EXIT_CLEAN and NO_COMPARISON in lines_local[0] and "OK" not in lines_local[0])
    boot = Observation(base_ref="main", current={"A::x"}, base="bootstrap", detail="new")
    expect("a genuine bootstrap passes, labelled as no comparison",
           decide(boot, True)[0] == EXIT_CLEAN and NO_COMPARISON in decide(boot, True)[1][0])

    if failures:
        for line in failures:
            log(f"  self-test FAIL: {line}")
        log(f"tenant-relation ratchet self-test: {len(failures)} case(s) failed — the comparator "
            "cannot be trusted, so no comparison was run.")
        return 3
    log("tenant-relation ratchet self-test: OK — additions detected, base selection event-aware, "
        "every no-comparison state refused in CI and labelled locally.")
    return 0
