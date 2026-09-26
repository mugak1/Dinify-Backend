"""A bounded harness over the REAL workflow files, for the claims a pure helper cannot make:
that an audit failure reaches the required ``test`` check, that no condition lets it be
skipped into a green aggregate, and that the shell a step runs under propagates the
audit's exit status.

  parse_yaml       a reader for the block-YAML subset these workflow files use (this
                   repository's dependencies include no YAML library). It is the Python
                   port of dependency-audit/tests/yaml-subset.mjs in Dinify-Frontend and
                   Dinify-Admin, which is held equal to the `yaml` library there; this port
                   was checked against PyYAML on every workflow file in all three
                   repositories when it was written. Anything outside the subset raises.
  simulate_job     GitHub's step-sequencing rules over a job's actual steps.
  run_step         executes a step's actual ``run:`` text the way a runner does with no
                   ``shell:`` (``bash -e {0}``: errexit, NO pipefail), with the audit
                   command replaced by a stub that exits with a chosen status.
  run_aggregator   executes the ``test`` aggregator job's own script with the matrix
                   result GitHub would substitute.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile


def _strip_comment(line):
    out, quote, i = "", None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            out += ch
            if quote == "'" and ch == "'" and i + 1 < len(line) and line[i + 1] == "'":
                out += "'"
                i += 2
                continue
            if quote == '"' and ch == "\\":
                out += line[i + 1] if i + 1 < len(line) else ""
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'" and (out.strip() == "" or re.search(r"[:\-\[{,]\s*$", out)):
            quote = ch
            out += ch
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out += ch
        i += 1
    return out.rstrip()


def _split_flow(inner):
    parts, depth, cur, quote = [], 0, "", None
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
            continue
        if ch in "[{":
            depth += 1
        if ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _scalar(text):
    t = text.strip()
    if t == "":
        return None
    if t.startswith('"'):
        if not t.endswith('"') or len(t) < 2:
            raise ValueError("unterminated string: %s" % t)
        return json.loads(t)
    if t.startswith("'"):
        if not t.endswith("'") or len(t) < 2:
            raise ValueError("unterminated string: %s" % t)
        return t[1:-1].replace("''", "'")
    if t.startswith("["):
        if not t.endswith("]"):
            raise ValueError("unterminated flow sequence: %s" % t)
        return [_scalar(p) for p in _split_flow(t[1:-1])]
    if t.startswith("{"):
        if not t.endswith("}"):
            raise ValueError("unterminated flow mapping: %s" % t)
        out = {}
        for part in _split_flow(t[1:-1]):
            m = re.match(r"^\s*([^:]+?)\s*:\s*(.*)$", part)
            if not m:
                raise ValueError("unsupported flow mapping entry: %s" % part)
            out[_scalar(m.group(1))] = _scalar(m.group(2))
        return out
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if t in ("null", "~"):
        return None
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", t):
        return int(t)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", t):
        return float(t)
    return t


_KEY = re.compile(r"^((?:\"[^\"]*\"|'[^']*'|[^\s\"'#][^:#]*?))\s*:(?:\s+(.*)|)$")


def parse_yaml(source):
    raw = source.replace("\r\n", "\n").split("\n")
    lines = [(n, text, len(text) - len(text.lstrip())) for n, text in enumerate(raw)]
    state = {"i": 0}

    def skip_blank():
        while state["i"] < len(lines) and _strip_comment(lines[state["i"]][1]).strip() == "":
            state["i"] += 1

    def block_scalar(header, parent_indent):
        folded = header.startswith(">")
        chomp = "strip" if header.endswith("-") else "keep" if header.endswith("+") else "clip"
        body, indent = [], None
        while state["i"] < len(lines):
            text = lines[state["i"]][1]
            if text.strip() == "":
                body.append("")
                state["i"] += 1
                continue
            ind = len(text) - len(text.lstrip())
            if ind <= parent_indent:
                break
            if indent is None:
                indent = ind
            if ind < indent:
                break
            body.append(text[indent:])
            state["i"] += 1
        while body and body[-1] == "":
            body.pop()
        if folded:
            value = ""
            for k, line in enumerate(body):
                if k == 0:
                    value = line
                    continue
                prev = body[k - 1]
                if line == "":
                    value += "\n"
                elif prev == "":
                    value += line
                elif line[:1].isspace() or prev[:1].isspace():
                    value += "\n" + line
                else:
                    value += " " + line
        else:
            value = "\n".join(body)
        return value if chomp == "strip" else value + "\n"

    def node(indent):
        skip_blank()
        if state["i"] >= len(lines):
            return None
        _, text, ind = lines[state["i"]]
        first = _strip_comment(text)
        if ind < indent:
            return None
        return sequence(ind) if (first.lstrip().startswith("- ") or first.strip() == "-") else mapping(ind)

    def value_after(rest, own_indent):
        if rest is None or rest == "":
            return node(own_indent + 1)
        if re.fullmatch(r"[|>][-+]?", rest):
            return block_scalar(rest, own_indent)
        return _scalar(rest)

    def mapping(indent, seed=None):
        out = seed if seed is not None else {}
        while True:
            skip_blank()
            if state["i"] >= len(lines):
                return out
            n, text, ind = lines[state["i"]]
            if ind != indent:
                if ind < indent:
                    return out
                raise ValueError("line %d: unexpected indentation" % (n + 1))
            stripped = _strip_comment(text).strip()
            if stripped.startswith("- "):
                return out
            m = _KEY.match(stripped)
            if not m:
                raise ValueError("line %d: not a mapping entry: %s" % (n + 1, stripped))
            state["i"] += 1
            out[_scalar(m.group(1))] = value_after(m.group(2), indent)

    def sequence(indent):
        out = []
        while True:
            skip_blank()
            if state["i"] >= len(lines):
                return out
            n, text, ind = lines[state["i"]]
            stripped = _strip_comment(text)
            if ind < indent or not stripped.lstrip().startswith("-"):
                return out
            if ind > indent:
                raise ValueError("line %d: unexpected indentation" % (n + 1))
            after = stripped.lstrip()[1:]
            content = after.lstrip()
            item_indent = indent + 1 + (len(after) - len(content))
            if content == "":
                state["i"] += 1
                out.append(node(indent + 1))
                continue
            m = _KEY.match(content)
            if m and not content.startswith(("\"", "'")):
                state["i"] += 1
                first = {_scalar(m.group(1)): value_after(m.group(2), item_indent)}
                out.append(mapping(item_indent, first))
            else:
                state["i"] += 1
                out.append(_scalar(content))

    doc = node(0)
    skip_blank()
    if state["i"] < len(lines):
        raise ValueError("line %d: could not be read" % (lines[state["i"]][0] + 1))
    return doc


def load_workflow(path):
    with open(path, "r", encoding="utf-8") as fh:
        return parse_yaml(fh.read())


_STATUS_FUNCTIONS = {
    "success()": lambda s: s == "success",
    "always()": lambda s: True,
    "failure()": lambda s: s == "failure",
    "cancelled()": lambda s: s == "cancelled",
    "!cancelled()": lambda s: s != "cancelled",
}


def simulate_job(steps, outcome_of):
    status, ran = "success", []
    for index, step in enumerate(steps):
        raw = "success()" if "if" not in step else re.sub(r"^\$\{\{\s*|\s*\}\}$", "", str(step["if"]).strip())
        gate = _STATUS_FUNCTIONS.get(raw)
        if gate is None:
            raise ValueError("unmodelled step condition on %r: %r" % (step.get("name"), step.get("if")))
        if not gate(status):
            ran.append((step.get("name"), "skipped"))
            continue
        out = outcome_of(step, index)
        ran.append((step.get("name"), out))
        if out == "cancelled":
            status = "cancelled"
        elif out == "failure" and step.get("continue-on-error") is not True and status == "success":
            status = "failure"
    return status, ran


def run_step(script, stub_exit, stub_name="python", shell=("bash", "-e")):
    """Run ``script`` under the runner's default shell with ``stub_name`` stubbed."""
    with tempfile.TemporaryDirectory(prefix="step-") as d:
        stub = os.path.join(d, stub_name)
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "stub %s $*" >&2\nexit %d\n' % (stub_name, int(stub_exit)))
        os.chmod(stub, 0o755)
        path = os.path.join(d, "step.sh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        r = subprocess.run(list(shell) + [path], cwd=d, env={"PATH": "%s:/usr/bin:/bin" % d, "HOME": d},
                           capture_output=True, text=True, timeout=30, check=False)
        return r.returncode


def run_aggregator(script, needs_result):
    """Execute the aggregator's script with every ``${{ needs.<job>.result }}``
    substituted, as GitHub does before the shell sees it. ``needs_result`` is one result
    for every job the aggregator needs, or ``{job: result}`` (a job left out of the map is
    substituted as ``success``, so a test states only the result it is about)."""
    def result_of(match):
        job = match.group(1)
        return needs_result.get(job, "success") if isinstance(needs_result, dict) else needs_result
    text = re.sub(r"\$\{\{\s*needs\.([A-Za-z0-9_-]+)\.result\s*\}\}", result_of, script)
    if "${{" in text:
        raise ValueError("the aggregator uses an expression this harness does not model")
    with tempfile.TemporaryDirectory(prefix="agg-") as d:
        path = os.path.join(d, "step.sh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return subprocess.run(["bash", "-e", path], capture_output=True, text=True, timeout=30, check=False).returncode


def status_swallowers(script):
    found = []
    if "|" in script:
        found.append("a pipe or `||`")
    if ";" in script:
        found.append("a `;` sequence")
    if re.search(r"\bset\s+\+e\b", script):
        found.append("`set +e`")
    if re.search(r"\btrue\b", script):
        found.append("`true`")
    if re.search(r"\bexit\s+0\b", script):
        found.append("`exit 0`")
    return found


def command_of(step):
    return step.get("run").strip() if isinstance(step.get("run"), str) else None
