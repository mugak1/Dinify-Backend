"""THE DEPENDENCY-AUDIT POLICY — the Python port of the one set of rules applied in every
Dinify repository.

Dinify-Frontend and Dinify-Admin implement the same rules in
``dependency-audit/lib/core.mjs``; ``conformance.json`` — byte-identical in all three
repositories, digest pinned in all three suites — is the oracle both implementations are
tested against. Change a rule here and the vectors fail until the same change lands in the
other ecosystem.

FOUR OUTCOMES, and they are not shades of one another:

    within_policy    the audit completed and nothing the policy blocks was found
    exceptions_only  the audit completed and passes ONLY because of specifically approved,
                     still-valid exception records — visible, never reported as clean
    blocking         the audit completed and found something the policy blocks, or a
                     disposition record was refused (expired, malformed, broadened,
                     mismatched, unused)
    incomplete       no trustworthy result exists: the scanner, the collection, the parse,
                     the inventory binding or the provenance failed, or a finding could not
                     be classified. An empty report is NOT a clean report.

THE RULES (the parent policy, D08 Stage B §6):
  - a CRITICAL or HIGH advisory blocks, whatever the scope;
  - ANY advisory on a RUNTIME package blocks, whatever the severity;
  - a MODERATE, LOW or INFO advisory confined to executable TOOLING is visible and needs
    an explicit triage record; untriaged it does not block, but it is counted and reported
    as TRIAGE REQUIRED — never a zero-findings result;
  - anything else (unknown severity on tooling, unknown scope below high) cannot be
    evaluated, and a policy that cannot be evaluated has not passed: incomplete.

pip-audit reports NO severity, so every Python finding reaches this module as severity
"unknown": on a runtime package it blocks, on tooling (pip) it is incomplete. That is the
conservative reading the policy requires, not an approximation of one.

Nothing here reads the network, the clock or the filesystem. ``now`` is an argument.
"""

from __future__ import annotations

import datetime as _dt
import re

OUTCOMES = ("within_policy", "exceptions_only", "blocking", "incomplete")
SEVERITIES = ("critical", "high", "moderate", "low", "info")
SCOPES = ("runtime", "tooling")
RECORD_KINDS = ("exception", "triage")

MAX_RECORD_DAYS = 90
MIN_TEXT = 20

_EXIT = {"within_policy": 0, "exceptions_only": 0, "blocking": 1, "incomplete": 2}

RECORD_KEYS = ("id", "kind", "advisory", "aliases", "package", "version", "paths", "scope",
               "applicability", "reason", "owner", "approval", "expires")
APPROVAL_KEYS = ("by", "reference", "date")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
_ADVISORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{2,127}")
_PACKAGE_RE = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._-]*")
# An EXACT version. Ranges, wildcards, tags and whitespace are how an exception quietly
# broadens to versions nobody reviewed.
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+)+([-+.!][0-9A-Za-z.+!-]+)?")
_PATH_RE = re.compile(r"(application|scanner):[^\s*?\[\]{}]+")
_REFERENCE_RE = re.compile(r"https://github\.com/mugak1/[A-Za-z0-9._-]+/(pull|issues)/[0-9]+(#[A-Za-z0-9_-]+)?")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DAY_MS = 86400000
_FINDING_KEYS = ("advisory", "package", "version", "path", "scope", "severity")


def exit_code_for(outcome):
    return _EXIT.get(outcome, 2)


def _full(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def parse_date(text):
    """A calendar date as epoch milliseconds, or None. 2026-02-30 is not a date."""
    if not isinstance(text, str):
        return None
    m = _DATE_RE.fullmatch(text)
    if not m:
        return None
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def parse_instant(text):
    """An ISO-8601 instant as epoch milliseconds, or None."""
    if not isinstance(text, str):
        return None
    try:
        value = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return int(value.timestamp() * 1000)


def _is_text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def validate_record(record, now_ms):
    """Structural validation of ONE disposition record; [] means well-formed and in date."""
    if not isinstance(record, dict):
        return ["record is not an object"]
    problems = []
    for key in record:
        if key not in RECORD_KEYS:
            problems.append('unknown field "%s"' % key)
    for key in RECORD_KEYS:
        if key not in record:
            problems.append('missing field "%s"' % key)
    if not _full(_ID_RE, record.get("id")):
        problems.append("id is not a valid identifier")
    if record.get("kind") not in RECORD_KINDS:
        problems.append("kind must be one of %s" % ", ".join(RECORD_KINDS))
    if not _full(_ADVISORY_RE, record.get("advisory")):
        problems.append("advisory is not an exact advisory identifier")
    aliases = record.get("aliases")
    if not isinstance(aliases, list) or not all(_full(_ADVISORY_RE, a) for a in aliases):
        problems.append("aliases must be a list of exact advisory identifiers (it may be empty)")
    if not _full(_PACKAGE_RE, record.get("package")):
        problems.append("package is not an exact package name")
    if not _full(_VERSION_RE, record.get("version")):
        problems.append("version is not one exact version")
    paths = record.get("paths")
    if not isinstance(paths, list) or not paths or not all(_full(_PATH_RE, p) for p in paths):
        problems.append("paths must be a non-empty list of exact graph paths")
    elif len(set(paths)) != len(paths):
        problems.append("paths repeats an entry")
    if record.get("scope") not in SCOPES:
        problems.append("scope must be one of %s" % ", ".join(SCOPES))
    if not _is_text(record.get("applicability"), MIN_TEXT):
        problems.append("applicability evidence must be at least %d characters" % MIN_TEXT)
    if not _is_text(record.get("reason"), MIN_TEXT):
        problems.append("reason must be at least %d characters" % MIN_TEXT)
    if not _is_text(record.get("owner")):
        problems.append("owner is required")
    approval = record.get("approval")
    approved_ms = None
    if not isinstance(approval, dict):
        problems.append("approval must record who approved it, where, and when")
    else:
        for key in approval:
            if key not in APPROVAL_KEYS:
                problems.append('unknown approval field "%s"' % key)
        if not _is_text(approval.get("by")):
            problems.append("approval.by is required")
        if not _full(_REFERENCE_RE, approval.get("reference")):
            problems.append("approval.reference must link the review that approved it (a mugak1 pull request or issue)")
        approved_ms = parse_date(approval.get("date"))
        if approved_ms is None:
            problems.append("approval.date is not a calendar date")
    expires_ms = parse_date(record.get("expires"))
    if expires_ms is None:
        problems.append("expires is not a calendar date")
    today_ms = (now_ms // _DAY_MS) * _DAY_MS
    if approved_ms is not None and approved_ms > today_ms:
        problems.append("approval.date is in the future")
    if approved_ms is not None and expires_ms is not None:
        if expires_ms <= approved_ms:
            problems.append("expires is not after approval.date")
        elif (expires_ms - approved_ms) / _DAY_MS > MAX_RECORD_DAYS:
            problems.append("expires is more than %d days after approval.date" % MAX_RECORD_DAYS)
    # Valid THROUGH the day before `expires`; from 00:00 UTC on that date it has lapsed.
    if expires_ms is not None and now_ms >= expires_ms:
        problems.append("expired on %s" % record.get("expires"))
    return problems


def classify(finding):
    """The policy decision for ONE finding, before any record is considered."""
    severity = finding.get("severity") if finding.get("severity") in SEVERITIES else "unknown"
    scope = finding.get("scope") if finding.get("scope") in SCOPES else "unknown"
    if severity in ("critical", "high"):
        return "blocking"
    if scope == "runtime":
        return "blocking"
    if scope == "tooling" and severity != "unknown":
        return "triage"
    return "unresolved"


def _identity(finding):
    """Which advisory a finding IS: its own identifier plus the aliases the SCANNER reported.

    A record's ``aliases`` are never part of this set. They are a claim the record makes,
    and trusting them let a record widen what it covered: a record for advisory A that
    listed B as an alias also covered a separate finding B at the same path, so one
    approval excepted a second, unapproved advisory and the audit exited 0."""
    aliases = finding.get("aliases") if isinstance(finding.get("aliases"), list) else []
    return {finding.get("advisory"), *aliases}


def _advisory_matches(record, finding):
    """A record names a finding only through the finding's own, scanner-reported identity."""
    return record.get("advisory") in _identity(finding)


def _uncorroborated_aliases(record, finding):
    """The aliases a record claims that the scanner does not report for this finding."""
    identity = _identity(finding)
    aliases = record.get("aliases") if isinstance(record.get("aliases"), list) else []
    return [a for a in aliases if a not in identity]


def _finding_problems(finding, index):
    if not isinstance(finding, dict):
        return ["finding %d is not an object" % index]
    missing = [k for k in _FINDING_KEYS if not (isinstance(finding.get(k), str) and finding.get(k) != "")]
    return ["finding %d lacks %s" % (index, ", ".join(missing))] if missing else []


def evaluate(incomplete=(), findings=(), records=(), now=None):
    """Evaluate normalized findings against the disposition records. See the module doc."""
    now_ms = parse_instant(now)
    reasons = [{"outcome": "incomplete", "code": i.get("code"), "detail": i.get("detail")} for i in incomplete]
    if now_ms is None:
        reasons.append({"outcome": "incomplete", "code": "clock", "detail": "the decision time is not an instant"})
    if not isinstance(findings, (list, tuple)):
        reasons.append({"outcome": "incomplete", "code": "findings", "detail": "findings is not a list"})
        findings = []
    if not isinstance(records, (list, tuple)):
        reasons.append({"outcome": "incomplete", "code": "records", "detail": "records is not a list"})
        records = []

    decided = []
    for index, finding in enumerate(findings):
        problems = _finding_problems(finding, index)
        for p in problems:
            reasons.append({"outcome": "incomplete", "code": "finding_malformed", "detail": p})
        base = dict(finding) if isinstance(finding, dict) else {}
        base.update({"class": "unresolved" if problems else classify(base), "disposition": "open", "coveredBy": None})
        decided.append(base)
    for f in decided:
        if f["class"] == "unresolved":
            reasons.append({"outcome": "incomplete", "code": "unresolved_finding",
                            "detail": '%s on %s: severity "%s" / scope "%s" cannot be evaluated by the policy'
                                      % (f.get("advisory", "?"), f.get("path", "?"), f.get("severity", "?"), f.get("scope", "?"))})

    # An id names ONE decision: every record sharing an id is refused.
    def id_of(record):
        return record.get("id") if isinstance(record, dict) and isinstance(record.get("id"), str) else "(no id)"
    counts_by_id = {}
    for record in records:
        counts_by_id[id_of(record)] = counts_by_id.get(id_of(record), 0) + 1
    checked = []
    for record in records:
        problems = validate_record(record, now_ms) if now_ms is not None else ["cannot be dated"]
        if counts_by_id[id_of(record)] > 1:
            problems.append("duplicate id")
        checked.append({"id": id_of(record), "record": record, "problems": problems, "used": set(), "applied": 0})

    for entry in checked:
        if entry["problems"]:
            continue
        r = entry["record"]
        related = [f for f in decided if f.get("package") == r["package"] and _advisory_matches(r, f)]
        if not related:
            entry["problems"].append("matches no current finding (stale: remove it)")
            continue
        for f in related:
            if f.get("path") not in r["paths"]:
                continue
            # A record describes ONE advisory as the scanner identifies it. An alias the
            # scanner does not report is an equivalence nobody has corroborated, so the
            # record is refused rather than read as a description of some other advisory.
            unverified = _uncorroborated_aliases(r, f)
            if unverified:
                entry["problems"].append("aliases %s are not reported by the scanner for %s at %s" % (", ".join(unverified), f.get("advisory"), f.get("path")))
                continue
            if f.get("version") != r["version"]:
                entry["problems"].append("version %s does not match %s at %s" % (r["version"], f.get("version"), f.get("path")))
                continue
            if f.get("scope") != r["scope"]:
                entry["problems"].append("scope %s does not match %s at %s" % (r["scope"], f.get("scope"), f.get("path")))
                continue
            if f["class"] == "unresolved":
                entry["problems"].append("%s is unresolved and cannot be excepted" % f.get("path"))
                continue
            wants = "exception" if f["class"] == "blocking" else "triage"
            if r["kind"] != wants:
                entry["problems"].append("a %s record cannot cover a %s finding at %s" % (r["kind"], f["class"], f.get("path")))
                continue
            entry["used"].add(f.get("path"))
        for p in r["paths"]:
            if p not in entry["used"]:
                entry["problems"].append("path %s matches no current finding for %s (broadened or stale)" % (p, r["advisory"]))

    for entry in checked:
        if entry["problems"]:
            continue
        r = entry["record"]
        for f in decided:
            if f.get("package") != r["package"] or not _advisory_matches(r, f) or f.get("path") not in r["paths"]:
                continue
            if f["coveredBy"]:
                entry["problems"].append("%s is already covered by %s" % (f.get("path"), f["coveredBy"]))
                continue
            f["coveredBy"] = r["id"]
            f["disposition"] = "excepted" if r["kind"] == "exception" else "triaged"
            entry["applied"] += 1
    for entry in checked:
        if not entry["problems"]:
            continue
        for f in decided:
            if f["coveredBy"] == entry["id"]:
                f["coveredBy"] = None
                f["disposition"] = "open"

    for entry in checked:
        if entry["problems"]:
            reasons.append({"outcome": "blocking", "code": "record_refused", "detail": "%s: %s" % (entry["id"], "; ".join(entry["problems"]))})
    for f in decided:
        if f["class"] == "blocking" and f["disposition"] == "open":
            reasons.append({"outcome": "blocking", "code": "blocking_finding",
                            "detail": "%s (%s) on %s@%s at %s [%s]" % (f.get("advisory"), f.get("severity"), f.get("package"),
                                                                     f.get("version"), f.get("path"), f.get("scope"))})

    counts = {
        "findings": len(decided),
        "blocking": sum(1 for f in decided if f["class"] == "blocking" and f["disposition"] == "open"),
        "excepted": sum(1 for f in decided if f["disposition"] == "excepted"),
        "triageRequired": sum(1 for f in decided if f["class"] == "triage" and f["disposition"] == "open"),
        "triaged": sum(1 for f in decided if f["disposition"] == "triaged"),
        "unresolved": sum(1 for f in decided if f["class"] == "unresolved"),
        "refusedRecords": sum(1 for e in checked if e["problems"]),
        "appliedRecords": sum(1 for e in checked if not e["problems"] and e["applied"] > 0),
    }
    if any(r["outcome"] == "incomplete" for r in reasons):
        outcome = "incomplete"
    elif any(r["outcome"] == "blocking" for r in reasons):
        outcome = "blocking"
    elif counts["excepted"] > 0:
        outcome = "exceptions_only"
    else:
        outcome = "within_policy"
    return {
        "outcome": outcome,
        "exitCode": exit_code_for(outcome),
        "counts": counts,
        "reasons": reasons,
        "findings": decided,
        "records": [{"id": e["id"], "status": "refused" if e["problems"] else "applied", "problems": e["problems"],
                     "covers": e["applied"]} for e in checked],
    }


def headline(result):
    """One sentence that cannot be mistaken for "no vulnerabilities"."""
    c = result["counts"]
    tail = ["no advisories reported for the audited inventory" if c["findings"] == 0 else "%d advisory finding(s)" % c["findings"]]
    if c["blocking"]:
        tail.append("%d BLOCKING" % c["blocking"])
    if c["excepted"]:
        tail.append("%d under approved exception" % c["excepted"])
    if c["triageRequired"]:
        tail.append("%d lower-severity tooling finding(s) REQUIRE TRIAGE (not zero findings)" % c["triageRequired"])
    if c["triaged"]:
        tail.append("%d triaged" % c["triaged"])
    if c["unresolved"]:
        tail.append("%d UNRESOLVED" % c["unresolved"])
    if c["refusedRecords"]:
        tail.append("%d disposition record(s) REFUSED" % c["refusedRecords"])
    label = {
        "within_policy": "AUDIT COMPLETE — WITHIN POLICY",
        "exceptions_only": "AUDIT COMPLETE — PASSES ONLY WITH APPROVED EXCEPTIONS",
        "blocking": "AUDIT COMPLETE — BLOCKING",
        "incomplete": "AUDIT UNAVAILABLE OR INCOMPLETE — NOT A CLEAN RESULT",
    }.get(result["outcome"], "AUDIT OUTCOME UNKNOWN")
    return "%s: %s" % (label, "; ".join(tail))
