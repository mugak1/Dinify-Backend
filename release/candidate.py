"""THE CANDIDATE'S SHAPE — the layout, the record schema, the evidence it must carry, and how
it is named. Shared by the producer that writes one and the consumer that refuses one, so
the two cannot disagree about what a candidate is.

    record.json          the small versioned record (``dinify.backend.candidate/1``)
    source.tar           ``git archive`` of the exact commit
    wheelhouse/*.whl     exactly the locked files, the bytes the environment was built from
    evidence/*           the dependency audit's retained evidence for that environment

The record is DATA about the other three and is not itself digested inside itself: its
integrity, and the GitHub artifact ID/digest of the upload that carries it, are facts a
later consumer establishes from the CI run's own artifact listing. Nothing here claims
that listing.

ELIGIBILITY IS NOT AUTHORITY. ``promotable: true`` is recorded only for a push to ``main``
of this repository, and means only that a later promotion-time assessment may CONSIDER the
candidate. A pull-request run validates a merge preview; its candidate is produced so the
producer and the reconstruction are exercised on every change, but it is named and recorded
non-promotable and must never be treated as a certified push to main.
"""

from __future__ import annotations

import hashlib
import json

RECORD_SCHEMA = "dinify.backend.candidate/1"
RECORD = "record.json"
SOURCE = "source.tar"
WHEELHOUSE = "wheelhouse"
EVIDENCE = "evidence"
REPOSITORY = "mugak1/Dinify-Backend"
WORKFLOW_PATH = ".github/workflows/ci.yml"
MAIN_REF = "refs/heads/main"

# The dependency audit's evidence set (dependency_audit/orchestrate.py writes exactly these).
EVIDENCE_FILES = (
    "snapshot.json", "collection.json", "result.json",
    "application.inventory-requirements.txt", "application.scanner-stdout.txt", "application.scanner-stderr.txt",
    "scanner.inventory-requirements.txt", "scanner.scanner-stdout.txt", "scanner.scanner-stderr.txt",
)

# Every one of these CI steps (by id) must have succeeded before a candidate may be packaged.
REQUIRED_STEPS = (
    "lock", "observe", "acquire", "install", "snapshot", "interpreter", "migrations", "money", "ambient",
    "ratchet", "guards", "audit-tests", "release-tests", "tenant", "suite", "audit",
)

ACCEPTED_AUDIT_OUTCOMES = ("within_policy", "exceptions_only")


def eligibility(event, ref, repository):
    """``(promotable, reason)`` from the run's own context."""
    if repository != REPOSITORY:
        return False, "produced in %s, not %s" % (repository, REPOSITORY)
    if event == "push" and ref == MAIN_REF:
        return True, "a push to main: eligible to be CONSIDERED by a promotion-time assessment; it authorizes nothing"
    if event == "pull_request":
        return False, "a pull-request run validates a merge preview; it is never a certified push to main"
    return False, "event %r on %r is not a push to main" % (event, ref)


def artifact_name(promotable, run_id, run_attempt, local=False):
    if local:
        return "backend-candidate-local"
    prefix = "backend-candidate" if promotable else "backend-candidate-nonpromotable"
    return "%s-%s-%s" % (prefix, run_id, run_attempt)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def listing_digest(listing):
    """A digest over ``[{filename, sha256, size}]`` — a file tree's identity without an
    archive's framing, so it does not change with timestamps or compression."""
    lines = "".join("%s\0%s\0%d\n" % (e["filename"], e["sha256"], e["size"]) for e in sorted(listing, key=lambda e: e["filename"]))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
