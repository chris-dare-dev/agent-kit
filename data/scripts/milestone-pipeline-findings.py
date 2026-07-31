#!/usr/bin/env python3
"""Findings register for the milestone pipeline — the single parser + single status
writer for Phase 3 critique findings (the per-finding analog of the roadmap
milestone register).

Register: <repo-root>/.claude/notes/milestones/<ID>/findings.json  (LOCAL-ONLY,
never committed — it lives inside the gitignored .claude/notes/ tier; policy
2026-07-09). Schema: data/references/milestone-pipeline-findings-schema.md.

Subcommands:

  extract --check <critique.md>...
      Format lint ONLY (no register write, no ID needed). Validates critique
      format v1.0: header block, per-finding required fields, 'Severity counts:'
      line matching the parsed totals. FAIL-LOUD: exit 1 listing every malformed
      block — never a silent skip (closes the dedupe-findings.py:52 gap).

  extract --id <ID> [--repo-root PATH] <critique.md>...
  extract --register <findings.json> <critique.md>...
      Parse critique file(s) -> findings.json. Same fail-loud lint first.
      Merge-safe: re-extract re-derives structure (title/file/line/severity)
      while PRESERVING status/resolution/history by finding id. Refuses drops
      of previously-registered ids (critiques are append-only post-creation;
      pass EVERY critique file of the run, not a subset).

  set <ID> <finding-ids> <fixed|deferred|invalidated> --resolution "..."
      The ONLY sanctioned status writer. <finding-ids> is one id or a
      comma-list (C1,H2,M1). --resolution is REQUIRED (fixed: rectification
      commit sha; deferred: deferral reason; invalidated: re-verification
      note). State machine (forward-only, audited into history[]):
        open -> fixed | deferred | invalidated
        deferred -> fixed          (a deferral later upgraded to a fix)
        fixed, invalidated         terminal
      Setting the current status again is an idempotent no-op (exit 0).

  gate <ID>
      Deterministic rectify gate: exit 3 while any CRITICAL/HIGH finding is
      'open' (listing them); WARNS (exit 0) about open MEDIUM/LOW findings
      (severity-scope decision: Chris Dare, 2026-07-09 — C+H block, M/L warn).
      No findings.json -> OK exit 0 (pre-register / ad-hoc runs gate nothing;
      same skip semantics as the roadmap dependency gate for unregistered ids).
      Enforcement site: milestone-pipeline-checkpoint.py invokes this at the
      'complete' transition; the command body also runs it BEFORE asking for
      push authorization (same script = one authority, two invocation points).

  summary <ID> [--field KEY] [--counts-for <critique.md>]
      Read-only JSON summary: counts by severity, per-file counts, and the
      open/fixed/deferred/invalidated id arrays (the orchestrator --set's
      state.json's fixed_findings/deferred_findings/invalidated_findings FROM
      these — no more hand-maintained arrays). --field prints one key's JSON;
      --counts-for prints {"critical":N,...} for ONE critique file (the state
      field critique_finding_counts keeps its existing meaning: the ADVERSARY
      file's counts — reconcile checks that file's 'Severity counts:' line).

  dedupe <critique.md>
      Cross-critic agreement callouts (absorbed from the retired
      milestone-pipeline-dedupe-findings.py; that filename remains as a thin
      deprecation stub). Same fail-loud parse first, THEN clusters findings
      within a 5-line window of the same file and inserts a
      '## Cross-critic agreement' section before '## Recommended rectification
      order'. Idempotent: skips if the section already exists.

  --self-test
      Embedded fixtures for every refusal path (run by CI Gate 1c).

set/gate/summary accept --register PATH to bypass <ID> resolution (used by
checkpoint.py, which already knows the state dir, and by the self-test).

Repo-root detection (in order): $REPO_ROOT env var; git rev-parse --show-toplevel
from CWD; walk up from this script's dir to nearest .git/.

Concurrency: exclusive fcntl flock on <findings.json>.lock around every
read-modify-write; writes are atomic (temp + os.replace).

Exit codes: 0 ok/no-op | 1 malformed critique, illegal transition, integrity
refusal, or not found | 2 usage | 3 gate refusal (open CRITICAL/HIGH).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# ---------------------------------------------------------------- the parser
# ONE parser, ONE format (critique format v1.0 —
# data/references/milestone-pipeline-critique-format.md). extract, --check,
# dedupe, and (via subprocess) checkpoint.py's complete-gate all run through
# these regexes. The finding-header regex is carried over verbatim from the
# retired dedupe-findings.py so the accepted id grammar does not drift.
#
# Canonical finding header. Examples:
#   **C1 -- Short title** (CRITICAL)
#   **H2 - Title** (HIGH)
#   **I-H1 -- Infra title** (HIGH)
#   **F-M1 — Frontend title** (MEDIUM)
#   **O-M1 -- OSS-scout title** (MEDIUM)
FINDING_RE = re.compile(
    r"^\*\*([A-Z]?-?[CHML]\d+)\s*(?:—|-+)\s*(.+?)\*\*\s*\(([A-Z]+)\)\s*$",
    re.MULTILINE,
)
# Tolerant File: line — the wild corpus (docs/*_critique.md) carries ranges
# (`path:33-35`), comma lists (`path:41,132,209`), file-only citations
# (`path` with prose), no-file citations (`File: n/a (runtime concern)`), and
# prose-led citations (`File: dispatch-stated `…` vs actual `…``) alongside
# the canonical `path:42`. The deterministic bar: the 'File:' LINE must exist
# in every finding block (the rectifier walks it); the machine 'file' field is
# best-effort — the FIRST backticked token on that line, with the line number
# taken from its ':N[:-,…]' suffix when present, else null.
FILE_LINE_RE = re.compile(r"^File:\s*(.*)$", re.MULTILINE)
BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")
FILE_LINE_SPLIT_RE = re.compile(r"^(.*?):(\d+)(?:[-,][0-9,\-\s]*)?$")
SOURCE_CRITIC_RE = re.compile(r"^\*\*Source critic:\*\*\s*([\w\-]+)", re.MULTILINE)
REGRESSION_GUARD_RE = re.compile(r"^\*\*Regression-guard:\*\*\s*(.+)$", re.MULTILINE)
# Remediation-contract body fields (round-4 F9): the Rectifier depends on these;
# corpus-calibrated 2026-07-09 — every finding in all 18 tracked v1.0 critiques
# carries all four (What/Why/Proposed fix/Regression-guard), so requiring them
# breaks nothing that ships.
BODY_FIELD_RES = {
    "What": re.compile(r"^\*\*What:\*\*", re.MULTILINE),
    "Why it matters": re.compile(r"^\*\*Why it matters:\*\*", re.MULTILINE),
    "Proposed fix": re.compile(r"^\*\*Proposed fix:\*\*", re.MULTILINE),
    "Regression-guard": REGRESSION_GUARD_RE,
}
# 'Severity counts:' — conditional critics emit prefixed variants
# (F-C0 F-H1 ... / I-C0 ...); the optional [A-Z]- groups absorb them.
SEVERITY_COUNTS_RE = re.compile(
    r"Severity counts:\*{0,2}\s*(?:[A-Z]-)?C(\d+)\s+(?:[A-Z]-)?H(\d+)"
    r"\s+(?:[A-Z]-)?M(\d+)\s+(?:[A-Z]-)?L(\d+)"
)
FORMAT_VERSION_RE = re.compile(r"^\*\*Critique format version:\*\*\s*1\.0\s*$", re.MULTILINE)
# Header block: '**Critic:**' (what every shipped critique and agent body emits)
# and '**Critic(s):**' (the reference doc's original spelling) are both valid.
HEADER_RES = {
    "Diff range": re.compile(r"^\*\*Diff range:\*\*", re.MULTILINE),
    "Critic": re.compile(r"^\*\*Critic(\(s\))?:\*\*", re.MULTILINE),
    "Generated": re.compile(r"^\*\*Generated:\*\*", re.MULTILINE),
}

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
LETTER_TO_SEVERITY = {"C": "CRITICAL", "H": "HIGH", "M": "MEDIUM", "L": "LOW"}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# Whole-string form of FINDING_RE's id group — register entries must satisfy the
# same grammar the parser emits (round-4 F2 strict-validation support).
FINDING_ID_RE = re.compile(r"^[A-Z]?-?[CHML]\d+$")

# Milestone-id containment (round-4 F10): the id becomes a path segment under
# .claude/notes/milestones/ — separators or a leading dot would escape the
# state dir ('../evil'). Same rule in milestone-pipeline-checkpoint.py and
# init-state.sh; keep the three in sync.
MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Near-miss finding headers (round-4 F1): a bold line that LOOKS like a finding
# header (severity-suffixed, or opening with an id-like token) but failed the
# canonical FINDING_RE must be a lint problem, not silence — a correlated
# double error (malformed header + counts line agreeing with the miscount)
# would otherwise drop the finding invisibly.
SUSPICIOUS_SEVERITY_RE = re.compile(
    r"^\*\*.*\*\*\s*\((?:CRITICAL|HIGH|MEDIUM|LOW)\)\s*$"
)
SUSPICIOUS_ID_RE = re.compile(r"^\*\*[A-Z]?-?[CHML]\d+\b")

STATUSES = {"open", "fixed", "deferred", "invalidated"}
# from-status -> set of legal to-statuses (forward-only; see docstring).
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "open": {"fixed", "deferred", "invalidated"},
    "deferred": {"fixed"},
    "fixed": set(),
    "invalidated": set(),
}

FENCE_CHARS = ("```", "~~~")


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks (``` / ~~~) before matching.

    The parser's regexes are line-anchored; without this, a finding-shaped
    example QUOTED inside a fence would either phantom-extract (self-consistent
    block) or falsely refuse the file (header alone -> counts mismatch) —
    adversary finding M2 (milestone-pipeline-uplift). Fenced lines are replaced
    with empty lines (line COUNT preserved; byte offsets are not — nothing
    downstream uses offsets: dedupe rewrites via marker string replacement).
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_fence and stripped.startswith(FENCE_CHARS):
            in_fence, fence_char = True, stripped[:3]
            out.append("\n" if line.endswith("\n") else "")
        elif in_fence:
            if stripped.startswith(fence_char):
                in_fence = False
            out.append("\n" if line.endswith("\n") else "")
        else:
            out.append(line)
    return "".join(out)


def parse_critique(text: str, label: str) -> tuple[list[dict], list[str]]:
    """Parse one critique file. Returns (findings, problems).

    problems is the fail-loud channel: ANY entry means the file drifts from
    format v1.0 — callers refuse rather than skip. Findings are still returned
    (best-effort) so error messages can cite what WAS parseable.

    Fenced code blocks are blanked before matching (see _strip_fences) —
    quoted finding-shaped examples neither phantom-extract nor falsely refuse.
    """
    text = _strip_fences(text)
    problems: list[str] = []
    findings: list[dict] = []

    for name, rx in HEADER_RES.items():
        if not rx.search(text):
            problems.append(f"{label}: missing '**{name}:**' header line")
    if not FORMAT_VERSION_RE.search(text):
        problems.append(
            f"{label}: missing '**Critique format version:** 1.0' header line "
            "(pre-v1.0 critiques are not lintable — see critique-format.md)"
        )

    matches = list(FINDING_RE.finditer(text))
    seen_ids: dict[str, int] = {}
    for i, m in enumerate(matches):
        fid, title, severity = m.group(1), m.group(2).strip(), m.group(3)
        block_start = m.start()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end]

        if fid in seen_ids:
            problems.append(f"{label}: duplicate finding id {fid}")
        seen_ids[fid] = 1

        if severity not in SEVERITIES:
            problems.append(
                f"{label}: {fid} has unknown severity '({severity})' — "
                f"expected one of {', '.join(SEVERITIES)}"
            )
        else:
            letter = fid.rstrip("0123456789")[-1:]
            if LETTER_TO_SEVERITY.get(letter) != severity:
                problems.append(
                    f"{label}: {fid} id-letter '{letter}' does not match "
                    f"severity '({severity})'"
                )

        fline_m = FILE_LINE_RE.search(block)
        if not fline_m:
            problems.append(
                f"{label}: {fid} has no 'File:' line "
                "(this is the block the old dedupe script silently skipped; "
                "use 'File: n/a (<why>)' when no file genuinely applies)"
            )
            fpath, fline = None, None
        else:
            token_m = BACKTICK_TOKEN_RE.search(fline_m.group(1))
            if not token_m:
                fpath, fline = None, None  # n/a-style or prose-only citation
            else:
                split = FILE_LINE_SPLIT_RE.match(token_m.group(1))
                if split:
                    fpath, fline = split.group(1), int(split.group(2))
                else:
                    fpath, fline = token_m.group(1), None

        critic = SOURCE_CRITIC_RE.search(block)
        if not critic:
            problems.append(f"{label}: {fid} has no '**Source critic:**' line")
        for fname, frx in BODY_FIELD_RES.items():
            if not frx.search(block):
                problems.append(
                    f"{label}: {fid} has no '**{fname}:**' line — the remediation "
                    "contract requires What / Why it matters / Proposed fix / "
                    "Regression-guard in every finding (round-4 F9)"
                )
        guard = REGRESSION_GUARD_RE.search(block)

        findings.append({
            "id": fid,
            "severity": severity if severity in SEVERITIES else None,
            "critic": critic.group(1) if critic else None,
            "file": fpath,
            "line": fline,
            "title": title,
            "regression_guard": guard.group(1).strip() if guard else None,
            # parser-internal (dedupe clustering); stripped before register write
            "_start": block_start,
            "_end": block_end,
        })

    # Near-miss scan (round-4 F1): any unfenced line that looks like a finding
    # header but did not parse canonically is a problem — never silence.
    for line in text.splitlines():
        if FINDING_RE.match(line):
            continue
        if SUSPICIOUS_SEVERITY_RE.match(line) or SUSPICIOUS_ID_RE.match(line):
            problems.append(
                f"{label}: line looks like a finding header but does not parse: "
                f"{line.strip()[:80]!r} — canonical form is "
                "'**<id> — <title>** (<SEVERITY>)' (em-dash or hyphens; not ':'). "
                "If this is prose, rephrase so the line does not open with a bold "
                "finding id / end with a bold severity tag, or fence it."
            )

    sev_lines = list(SEVERITY_COUNTS_RE.finditer(text))
    if not sev_lines:
        problems.append(f"{label}: no 'Severity counts: C_ H_ M_ L_' line")
    else:
        if len(sev_lines) > 1:
            problems.append(
                f"{label}: {len(sev_lines)} 'Severity counts:' lines found — exactly "
                "one is allowed (a stale duplicate misleads human readers even though "
                "first-match tooling would agree with itself)"
            )
        declared = dict(zip(("critical", "high", "medium", "low"), map(int, sev_lines[0].groups())))
        parsed = {k: 0 for k in ("critical", "high", "medium", "low")}
        for f in findings:
            if f["severity"]:
                parsed[f["severity"].lower()] += 1
        if declared != parsed:
            problems.append(
                f"{label}: 'Severity counts:' line declares {declared} but "
                f"{parsed} finding blocks parsed — the counts line and the "
                "blocks must agree (fix whichever is wrong; never hand-wave)"
            )

    return findings, problems


# ----------------------------------------------------------------- plumbing


def _find_repo_root() -> Path:
    env = os.environ.get("REPO_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        if out:
            return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    sys.exit("could not determine repo root. Set REPO_ROOT env var or run inside a git repo.")


def _register_path(mid: str | None, register: str | None, repo_root: str | None) -> Path:
    if register:
        return Path(register)
    if not mid:
        sys.exit("pass <ID> or --register PATH")
    if not MILESTONE_ID_RE.match(mid) or "/" in mid or "\\" in mid:
        sys.exit(
            f"invalid milestone id {mid!r} — ids are [A-Za-z0-9][A-Za-z0-9._-]* "
            "(no path separators; the id is a directory segment under "
            ".claude/notes/milestones/)"
        )
    root = Path(repo_root) if repo_root else _find_repo_root()
    return root / ".claude" / "notes" / "milestones" / mid / "findings.json"


@contextmanager
def _locked(path: Path):
    """Exclusive advisory lock for the read-modify-write window."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_register(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"findings register not found: {path} — run 'extract' first")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"not valid JSON: {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        sys.exit(f"not a findings register (no 'findings' array): {path}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        sys.exit(
            f"unsupported schema_version {doc.get('schema_version')!r} in {path} — "
            "this writer only mutates v1 (see milestone-pipeline-findings-schema.md)"
        )
    # STRICT per-finding validation (round-4 F2): every consumer (gate, set,
    # summary, extract's merge path — and checkpoint 'complete' via gate) loads
    # through here, so malformed entries FAIL LOUD everywhere instead of
    # fail-opening the gate (a hand-edited status like "opened" used to read as
    # non-blocking on a CRITICAL). Only sanctioned writers produce this file;
    # anything else is refused, never reinterpreted.
    problems: list[str] = []
    seen_ids: set[str] = set()
    for i, f in enumerate(doc["findings"]):
        if not isinstance(f, dict):
            problems.append(f"findings[{i}] is not an object")
            continue
        fid = f.get("id")
        label = fid if isinstance(fid, str) else f"findings[{i}]"
        if not isinstance(fid, str) or not FINDING_ID_RE.match(fid):
            problems.append(f"{label}: id {fid!r} does not match the finding-id grammar")
        elif fid in seen_ids:
            problems.append(f"{label}: duplicate finding id")
        else:
            seen_ids.add(fid)
        if f.get("severity") not in SEVERITIES:
            problems.append(f"{label}: severity {f.get('severity')!r} not in {SEVERITIES}")
        if f.get("status") not in STATUSES:
            problems.append(
                f"{label}: status {f.get('status')!r} not in {sorted(STATUSES)} "
                "(hand-edited register? fix it or delete + re-run extract)"
            )
    if problems:
        sys.exit(
            f"malformed findings register {path} — refusing (fail-loud, "
            "never fail-open):\n  - " + "\n  - ".join(problems)
        )
    return doc


def _save_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _public(finding: dict) -> dict:
    return {k: v for k, v in finding.items() if not k.startswith("_")}


def _parse_files(paths: list[str]) -> tuple[list[dict], list[str]]:
    """Parse every critique file; cross-file duplicate ids are also problems."""
    all_findings: list[dict] = []
    problems: list[str] = []
    by_id: dict[str, str] = {}
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            problems.append(f"{p}: file not found")
            continue
        findings, probs = parse_critique(fp.read_text(encoding="utf-8"), p)
        problems.extend(probs)
        for f in findings:
            f["critique_file"] = p
            if f["id"] in by_id:
                problems.append(
                    f"{p}: finding id {f['id']} already emitted by {by_id[f['id']]} "
                    "(ids must be unique across the run's critique files)"
                )
            else:
                by_id[f["id"]] = p
        all_findings.extend(findings)
    return all_findings, problems


# -------------------------------------------------------------- subcommands


def cmd_extract(args: argparse.Namespace) -> int:
    findings, problems = _parse_files(args.files)
    if problems:
        print(
            f"extract: REFUSED — {len(problems)} format problem(s) "
            "(critique format v1.0 — fix the file(s), never skip):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    counts = {k: 0 for k in ("critical", "high", "medium", "low")}
    for f in findings:
        counts[f["severity"].lower()] += 1
    tally = f"C{counts['critical']} H{counts['high']} M{counts['medium']} L{counts['low']}"

    if args.check:
        print(f"check: OK — {len(findings)} finding(s) ({tally}) across {len(args.files)} file(s)")
        return 0

    reg_path = _register_path(args.id, args.register, args.repo_root)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    with _locked(reg_path):
        existing: dict[str, dict] = {}
        prev_milestone_id = ""
        if reg_path.exists():
            doc = _load_register(reg_path)
            prev_milestone_id = doc.get("milestone_id") or ""
            existing = {f["id"]: f for f in doc["findings"] if isinstance(f, dict)}
            dropped = sorted(set(existing) - {f["id"] for f in findings})
            if dropped:
                print(
                    "extract: REFUSED — these registered finding ids are absent "
                    f"from the given critique file(s): {', '.join(dropped)}.\n"
                    "Critiques are append-only post-creation; pass EVERY critique "
                    "file of the run (adversary + conditional critics), and never "
                    "hand-delete finding blocks.",
                    file=sys.stderr,
                )
                return 1
            # A zero-finding critique (clean conditional critic) carries no ids,
            # so the id-based drop check above cannot protect it — refuse
            # critique_files shrinkage explicitly (adversary finding L2).
            dropped_files = sorted(
                {os.path.normpath(p) for p in doc.get("critique_files", [])}
                - {os.path.normpath(p) for p in args.files}
            )
            if dropped_files:
                print(
                    "extract: REFUSED — these registered critique files are absent "
                    f"from this invocation: {', '.join(dropped_files)}.\n"
                    "Pass EVERY critique file of the run — a zero-finding critique "
                    "is still evidence and must stay registered.",
                    file=sys.stderr,
                )
                return 1
        merged: list[dict] = []
        preserved = 0
        for f in findings:
            pub = _public(f)
            prev = existing.get(f["id"])
            if prev:
                pub["status"] = prev.get("status", "open")
                pub["resolution"] = prev.get("resolution")
                pub["history"] = prev.get("history", [])
                preserved += 1
            else:
                pub["status"] = "open"
                pub["resolution"] = None
                pub["history"] = [{"at": now, "from": None, "to": "open", "reason": "extracted"}]
            merged.append(pub)
        doc = {
            "schema_version": SCHEMA_VERSION,
            "milestone_id": args.id or prev_milestone_id,
            "critique_files": list(args.files),
            "generated_by": "milestone-pipeline-findings.py extract",
            "generated_at": now,
            "updated_at": now,
            "findings": merged,
        }
        _save_atomic(reg_path, doc)
    note = f", statuses preserved for {preserved}" if preserved else ""
    print(f"extracted {len(merged)} finding(s) ({tally}) from {len(args.files)} file(s){note} -> {reg_path}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    ids = [s.strip() for s in args.finding_ids.split(",") if s.strip()]
    if not ids:
        sys.exit("set: no finding ids given")
    if not args.resolution or not args.resolution.strip():
        sys.exit(
            "set: --resolution is REQUIRED (fixed: rectification commit sha; "
            "deferred: deferral reason; invalidated: re-verification note)"
        )
    reg_path = _register_path(args.id, args.register, args.repo_root)
    now = _now()
    with _locked(reg_path):
        doc = _load_register(reg_path)
        by_id = {f["id"]: f for f in doc["findings"] if isinstance(f, dict)}
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            sys.exit(
                f"set: unknown finding id(s): {', '.join(unknown)}. "
                f"Register has: {', '.join(sorted(by_id))}"
            )
        changed = 0
        for fid in ids:
            f = by_id[fid]
            cur = f.get("status", "open")
            if cur == args.status:
                print(f"{fid}: already {args.status} — no-op")
                continue
            if args.status not in STATUS_TRANSITIONS.get(cur, set()):
                sys.exit(
                    f"set: illegal transition for {fid}: {cur} -> {args.status} "
                    "(open -> fixed|deferred|invalidated; deferred -> fixed; "
                    "fixed/invalidated are terminal)"
                )
            f["status"] = args.status
            f["resolution"] = args.resolution.strip()
            f.setdefault("history", []).append(
                {"at": now, "from": cur, "to": args.status, "reason": args.resolution.strip()}
            )
            changed += 1
        if changed:
            doc["updated_at"] = now
            _save_atomic(reg_path, doc)
    print(f"set: {changed} finding(s) -> {args.status} ({reg_path})")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    reg_path = _register_path(args.id, args.register, args.repo_root)
    if not reg_path.exists():
        print(
            f"gate: OK — no findings register at {reg_path} "
            "(pre-register or ad-hoc run); nothing to gate"
        )
        return 0
    doc = _load_register(reg_path)
    open_ch = [
        f for f in doc["findings"]
        if f.get("status") == "open" and f.get("severity") in ("CRITICAL", "HIGH")
    ]
    open_ml = [
        f for f in doc["findings"]
        if f.get("status") == "open" and f.get("severity") in ("MEDIUM", "LOW")
    ]
    if open_ch:
        print(
            f"gate: REFUSED — {len(open_ch)} open CRITICAL/HIGH finding(s):",
            file=sys.stderr,
        )
        for f in sorted(open_ch, key=lambda x: SEVERITY_ORDER.get(x.get("severity"), 9)):
            print(f"  - {f['id']} ({f['severity']}, open): {f['title']}", file=sys.stderr)
        print(
            "Fix or invalidate each (re-verify against live code first):\n"
            "  milestone-pipeline-findings.py set <ID> <finding-id> fixed|invalidated --resolution \"...\"",
            file=sys.stderr,
        )
        return 3
    print(f"gate: PASS — no open CRITICAL/HIGH findings ({reg_path})")
    if open_ml:
        print(f"WARN: {len(open_ml)} open MEDIUM/LOW finding(s) have no recorded disposition:")
        for f in open_ml:
            print(f"  - {f['id']} ({f['severity']}, open): {f['title']}")
        print("  Defer with a recorded reason: ... set <ID> <ids> deferred --resolution \"<why>\"")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    reg_path = _register_path(args.id, args.register, args.repo_root)
    doc = _load_register(reg_path)
    findings = [f for f in doc["findings"] if isinstance(f, dict)]

    if args.counts_for:
        # Match tolerantly across absolute/relative spellings of the same file
        # (adversary finding L4): compare resolved paths (relative entries are
        # resolved against CWD first, then the repo root the register lives
        # under: <repo-root>/.claude/notes/milestones/<id>/findings.json).
        reg_root = reg_path.resolve().parent.parent.parent.parent.parent

        def _candidates(p: str) -> set[str]:
            cands = {os.path.normpath(p)}
            if os.path.isabs(p):
                cands.add(str(Path(p).resolve()))
            else:
                cands.add(str((Path.cwd() / p).resolve()))
                cands.add(str((reg_root / p).resolve()))
            return cands

        want = _candidates(args.counts_for)
        match = None
        for reg_file in doc.get("critique_files", []):
            if want & _candidates(reg_file):
                match = reg_file
                break
        if match is None:
            sys.exit(
                f"summary: '{args.counts_for}' is not among this run's critique "
                f"files: {sorted(doc.get('critique_files', []))}"
            )
        counts = {k: 0 for k in ("critical", "high", "medium", "low")}
        for f in findings:
            if f.get("critique_file") == match and f.get("severity"):
                counts[f["severity"].lower()] += 1
        print(json.dumps(counts))
        return 0

    counts = {k: 0 for k in ("critical", "high", "medium", "low")}
    per_file: dict[str, dict] = {}
    arrays: dict[str, list[str]] = {"open": [], "fixed": [], "deferred": [], "invalidated": []}
    for f in findings:
        if f.get("severity"):
            counts[f["severity"].lower()] += 1
            pf = per_file.setdefault(
                f.get("critique_file", "?"), {k: 0 for k in ("critical", "high", "medium", "low")}
            )
            pf[f["severity"].lower()] += 1
        arrays.setdefault(f.get("status", "open"), []).append(f["id"])
    out = {
        "milestone_id": doc.get("milestone_id"),
        "counts": counts,
        "per_file": per_file,
        **arrays,
    }
    if args.field:
        if args.field not in out:
            sys.exit(f"summary: unknown field '{args.field}'. Valid: {', '.join(out)}")
        print(json.dumps(out[args.field]))
    else:
        print(json.dumps(out, indent=2))
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")
    if "## Cross-critic agreement" in text:
        print(f"{path}: already deduped (skipping)")
        return 0

    findings, problems = parse_critique(text, str(path))
    if problems:
        print(
            f"dedupe: REFUSED — {len(problems)} format problem(s) "
            "(the old script silently skipped these; now they block):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    # Cluster findings within a 5-line window of the same file. Findings whose
    # File: citation has no line number cannot be windowed — they are excluded
    # from clustering (NOT from extraction; that distinction is the point).
    by_file: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        if f["file"] and f["line"] is not None:
            by_file[f["file"]].append(f)
    clusters: list[list[dict]] = []
    for file_findings in by_file.values():
        file_findings.sort(key=lambda f: f["line"])
        cur: list[dict] = []
        for f in file_findings:
            if cur and f["line"] - cur[-1]["line"] <= 5:
                cur.append(f)
            else:
                if len(cur) > 1:
                    clusters.append(cur)
                cur = [f]
        if len(cur) > 1:
            clusters.append(cur)

    if not clusters:
        print(f"{path}: {len(findings)} findings; no duplicate clusters found")
        return 0

    callouts = ["", "## Cross-critic agreement", ""]
    callouts.append(
        "The following findings cluster within 5 lines of each other in the same file. "
        "Multiple critics flagged the same area -- these are the strongest signals to fix first.\n"
    )
    for cl in clusters:
        ids = ", ".join(f["id"] for f in cl)
        sev = min(cl, key=lambda f: SEVERITY_ORDER.get(f["severity"], 99))["severity"]
        loc = f"{cl[0]['file']}:{cl[0]['line']}-{cl[-1]['line']}"
        sources = sorted({f["critic"] for f in cl})
        titles = "; ".join(f["title"] for f in cl)
        callouts.append(f"- **{ids}** at `{loc}` ({sev}) [{', '.join(sources)}]: {titles}")

    marker = "## Recommended rectification order"
    if marker in text:
        rewritten = text.replace(marker, "\n".join(callouts) + "\n\n" + marker, 1)
    else:
        rewritten = text.rstrip() + "\n\n" + "\n".join(callouts) + "\n"
    path.write_text(rewritten, encoding="utf-8")
    print(
        f"{path}: deduped -- {len(clusters)} cluster(s) covering "
        f"{sum(len(c) for c in clusters)} findings"
    )
    return 0


# ---------------------------------------------------------------- self-test

VALID_CRITIQUE = """# Adversary critique — milestone demo-m1

**Diff range:** aaa1111..bbb2222
**Critic:** adversary
**Generated:** 2026-07-09T00:00:00Z
**Critique format version:** 1.0

## Executive summary

- **Overall verdict:** DO-NOT-SHIP
- **Severity counts:** C1 H0 M1 L0
- **Headline finding:** demo

**C1 — Hardcoded push in CI job** (CRITICAL)

File: `ci/deploy.yml:12`

**What:** Auto-push.

**Why it matters:** External write without authorization.

**Proposed fix:** Gate it.

**Regression-guard:** tests/test_gate.py

**Source critic:** adversary

**M1 — Doc drift on convention** (MEDIUM)

File: `README.md`

**What:** Stale doc.

**Why it matters:** Convention is load-bearing.

**Proposed fix:** Update it.

**Regression-guard:** (none feasible)

**Source critic:** adversary

## What was done well

- bullet

## Recommended rectification order

1. C1 — fix first
"""


def self_test() -> int:
    import tempfile

    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {name}: {'ok' if ok else f'FAIL {detail}'}")
        if not ok:
            failures += 1

    def run_main(argv: list[str]) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(argv)
            except SystemExit as exc:  # sys.exit(str) -> rc 1 with message
                if isinstance(exc.code, int) or exc.code is None:
                    rc = exc.code or 0
                else:
                    err.write(str(exc.code) + "\n")
                    rc = 1
        return rc, out.getvalue(), err.getvalue()

    print("self-test: milestone-pipeline-findings.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "docs").mkdir()
        reg = root / "findings.json"
        crit = root / "docs" / "demo-m1_adversary_critique.md"
        crit.write_text(VALID_CRITIQUE, encoding="utf-8")
        rel_crit = str(crit)

        # 1. --check passes a valid v1.0 critique
        rc, out, err = run_main(["extract", "--check", rel_crit])
        expect("check: valid critique passes", rc == 0 and "2 finding(s)" in out, f"rc={rc} {err}")

        # 2. malformed: missing File: line fails loud, names the id
        bad = VALID_CRITIQUE.replace("File: `README.md`\n\n", "")
        p = root / "docs" / "bad1.md"
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: missing File: fails loud", rc == 1 and "M1" in err, f"rc={rc}")

        # 3. counts-line mismatch fails
        bad = VALID_CRITIQUE.replace("C1 H0 M1 L0", "C1 H1 M1 L0")
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: counts mismatch fails", rc == 1 and "Severity counts" in err, f"rc={rc}")

        # 4. missing counts line fails
        bad = VALID_CRITIQUE.replace("- **Severity counts:** C1 H0 M1 L0\n", "")
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: missing counts line fails", rc == 1 and "no 'Severity counts" in err, f"rc={rc}")

        # 5. missing format-version header fails
        bad = VALID_CRITIQUE.replace("**Critique format version:** 1.0\n", "")
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: missing format version fails", rc == 1 and "format version" in err, f"rc={rc}")

        # 6. id-letter / severity mismatch fails
        bad = VALID_CRITIQUE.replace("**M1 — Doc drift on convention** (MEDIUM)",
                                     "**M1 — Doc drift on convention** (HIGH)")
        bad = bad.replace("C1 H0 M1 L0", "C1 H1 M0 L0")
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: id/severity mismatch fails", rc == 1 and "id-letter" in err, f"rc={rc}")

        # 7. duplicate id fails
        bad = VALID_CRITIQUE.replace("**M1 — Doc drift on convention** (MEDIUM)",
                                     "**C1 — Doc drift on convention** (CRITICAL)")
        bad = bad.replace("C1 H0 M1 L0", "C2 H0 M0 L0")
        p.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: duplicate id fails", rc == 1 and "duplicate" in err, f"rc={rc}")

        # 8. prefixed counts line (conditional critics) parses
        pref = VALID_CRITIQUE.replace("C1 H0 M1 L0", "F-C1 F-H0 F-M1 F-L0")
        p.write_text(pref, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: F- prefixed counts line parses", rc == 0, f"rc={rc} {err}")

        # 9. extract writes the register (all open)
        rc, out, err = run_main(["extract", "--id", "demo-m1", "--register", str(reg), rel_crit])
        doc = json.loads(reg.read_text(encoding="utf-8"))
        expect(
            "extract: register written, statuses open",
            rc == 0 and len(doc["findings"]) == 2
            and all(f["status"] == "open" for f in doc["findings"]),
            f"rc={rc}",
        )

        # 10. set: resolution required
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "C1", "fixed"])
        expect("set: refuses without --resolution", rc != 0, f"rc={rc}")

        # 11. set: unknown id refused
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "H9", "fixed",
                                 "--resolution", "x"])
        expect("set: unknown id refused", rc != 0 and "H9" in err, f"rc={rc} {err}")

        # 12. gate refuses while C1 open (exit 3)
        rc, out, err = run_main(["gate", "demo-m1", "--register", str(reg)])
        expect("gate: open CRITICAL refuses (exit 3)", rc == 3 and "C1" in err, f"rc={rc}")

        # 13. set C1 fixed; gate passes but warns on open M1
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "C1", "fixed",
                                 "--resolution", "abc1234"])
        expect("set: open -> fixed ok", rc == 0, f"rc={rc} {err}")
        rc, out, err = run_main(["gate", "demo-m1", "--register", str(reg)])
        expect("gate: passes with WARN on open MEDIUM", rc == 0 and "WARN" in out and "M1" in out,
               f"rc={rc}")

        # 14. illegal transition fixed -> deferred refused
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "C1", "deferred",
                                 "--resolution", "nah"])
        expect("set: fixed -> deferred refused", rc != 0 and "illegal" in err, f"rc={rc}")

        # 15. idempotent same-status no-op
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "C1", "fixed",
                                 "--resolution", "abc1234"])
        expect("set: same-status no-op", rc == 0 and "no-op" in out, f"rc={rc}")

        # 16. deferred -> fixed allowed (comma-list set)
        rc, out, err = run_main(["set", "--register", str(reg), "demo-m1", "M1", "deferred",
                                 "--resolution", "test surface too large"])
        rc2, out2, err2 = run_main(["set", "--register", str(reg), "demo-m1", "M1", "fixed",
                                    "--resolution", "def5678"])
        expect("set: deferred -> fixed allowed", rc == 0 and rc2 == 0, f"rc={rc}/{rc2}")

        # 17. re-extract preserves statuses (merge-safe)
        rc, out, err = run_main(["extract", "--id", "demo-m1", "--register", str(reg), rel_crit])
        doc = json.loads(reg.read_text(encoding="utf-8"))
        by_id = {f["id"]: f for f in doc["findings"]}
        expect(
            "extract: re-extract preserves statuses",
            rc == 0 and by_id["C1"]["status"] == "fixed" and by_id["M1"]["status"] == "fixed",
            f"rc={rc}",
        )

        # 18. extract refuses dropped ids (subset of critique files / hand-edit)
        sub = VALID_CRITIQUE.replace(
            "**M1 — Doc drift on convention** (MEDIUM)\n\nFile: `README.md`\n\n"
            "**What:** Stale doc.\n\n**Why it matters:** Convention is load-bearing.\n\n"
            "**Proposed fix:** Update it.\n\n**Regression-guard:** (none feasible)\n\n"
            "**Source critic:** adversary\n\n", "")
        sub = sub.replace("C1 H0 M1 L0", "C1 H0 M0 L0")
        p.write_text(sub, encoding="utf-8")
        rc, out, err = run_main(["extract", "--id", "demo-m1", "--register", str(reg), str(p)])
        expect("extract: refuses dropped ids", rc == 1 and "M1" in err, f"rc={rc}")

        # 19. summary: --counts-for + --field
        rc, out, err = run_main(["summary", "demo-m1", "--register", str(reg),
                                 "--counts-for", rel_crit])
        counts_ok = rc == 0 and json.loads(out) == {"critical": 1, "high": 0, "medium": 1, "low": 0}
        expect("summary: --counts-for", counts_ok, f"rc={rc} out={out.strip()}")
        rc, out, err = run_main(["summary", "demo-m1", "--register", str(reg), "--field", "fixed"])
        expect("summary: --field fixed", rc == 0 and set(json.loads(out)) == {"C1", "M1"},
               f"rc={rc} out={out.strip()}")
        rc, out, err = run_main(["summary", "demo-m1", "--register", str(reg),
                                 "--counts-for", "docs/not-a-file.md"])
        expect("summary: --counts-for unknown file refused", rc != 0, f"rc={rc}")

        # 20. gate: absent register is OK exit 0 (pre-register / ad-hoc runs)
        rc, out, err = run_main(["gate", "demo-m1", "--register", str(root / "nope.json")])
        expect("gate: absent register no-ops OK", rc == 0 and "nothing to gate" in out, f"rc={rc}")

        # 21. dedupe: clusters within 5 lines, inserts before the marker, idempotent
        two = VALID_CRITIQUE.replace("File: `README.md`", "File: `ci/deploy.yml:14`")
        dd = root / "docs" / "dedupe.md"
        dd.write_text(two, encoding="utf-8")
        rc, out, err = run_main(["dedupe", str(dd)])
        text = dd.read_text(encoding="utf-8")
        expect(
            "dedupe: cluster emitted before rectification order",
            rc == 0 and "## Cross-critic agreement" in text
            and text.index("Cross-critic agreement") < text.index("Recommended rectification order"),
            f"rc={rc}",
        )
        rc, out, err = run_main(["dedupe", str(dd)])
        expect("dedupe: idempotent second run", rc == 0 and "already deduped" in out, f"rc={rc}")

        # 22. dedupe fails loud on malformed (the old silent-skip gap)
        bad = VALID_CRITIQUE.replace("File: `ci/deploy.yml:12`\n\n", "")
        dd2 = root / "docs" / "dedupe-bad.md"
        dd2.write_text(bad, encoding="utf-8")
        rc, out, err = run_main(["dedupe", str(dd2)])
        expect("dedupe: malformed block fails loud", rc == 1 and "C1" in err, f"rc={rc}")

        # 23. fenced finding-shaped examples are IGNORED (adversary M2): a
        # self-consistent quoted block must neither phantom-extract nor refuse.
        fenced = VALID_CRITIQUE.replace(
            "## What was done well",
            "Example (quoted, must not parse):\n\n```markdown\n"
            "**C9 — Quoted example** (CRITICAL)\n\nFile: `x.py:1`\n\n"
            "**Source critic:** adversary\n\n- **Severity counts:** C9 H9 M9 L9\n```\n\n"
            "## What was done well",
        )
        p.write_text(fenced, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: fenced finding block ignored (M2)", rc == 0 and "2 finding(s)" in out,
               f"rc={rc} {err[:120]}")

        # 24. duplicate 'Severity counts:' lines refused (adversary L1)
        dup = VALID_CRITIQUE.replace(
            "- **Headline finding:** demo",
            "- **Headline finding:** demo\n- **Severity counts:** C9 H9 M9 L9",
        )
        p.write_text(dup, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: duplicate counts lines refused (L1)", rc == 1 and "exactly" in err,
               f"rc={rc} {err[:120]}")

        # 25. critique_files shrinkage refused even when the dropped file had
        # zero findings (adversary L2)
        clean = (
            "# Infra critique — milestone demo-m1\n\n"
            "**Diff range:** aaa1111..bbb2222\n**Critic:** infra-safety\n"
            "**Generated:** 2026-07-09T00:00:00Z\n**Critique format version:** 1.0\n\n"
            "## Executive summary\n\n- **Overall verdict:** SHIP\n"
            "- **Severity counts:** I-C0 I-H0 I-M0 I-L0\n"
        )
        zf = root / "docs" / "demo-m1_infra_critique.md"
        zf.write_text(clean, encoding="utf-8")
        reg2 = root / "findings2.json"
        rc, out, err = run_main(["extract", "--id", "demo-m1", "--register", str(reg2),
                                 rel_crit, str(zf)])
        expect("extract: zero-finding file registers", rc == 0, f"rc={rc} {err[:120]}")
        rc, out, err = run_main(["extract", "--id", "demo-m1", "--register", str(reg2), rel_crit])
        expect("extract: critique_files shrinkage refused (L2)",
               rc == 1 and "demo-m1_infra_critique.md" in err, f"rc={rc} {err[:160]}")

        # 26. summary --counts-for tolerates absolute-vs-relative spellings (adversary L4)
        rc, out, err = run_main(["summary", "demo-m1", "--register", str(reg2),
                                 "--counts-for", str(crit.resolve())])
        expect("summary: --counts-for absolute path resolves (L4)",
               rc == 0 and json.loads(out)["critical"] == 1, f"rc={rc} {err[:120]}")

        # 27. near-miss finding headers refuse (round-4 F1): colon delimiter +
        # counts line consistent with the MISCOUNT (the correlated double error)
        near = VALID_CRITIQUE.replace("**M1 — Doc drift on convention** (MEDIUM)",
                                      "**M1: Doc drift on convention** (MEDIUM)")
        near = near.replace("C1 H0 M1 L0", "C1 H0 M0 L0")
        p.write_text(near, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: near-miss header refused (F1)",
               rc == 1 and "looks like a finding header" in err, f"rc={rc} {err[:120]}")

        # 28. malformed register entries fail loud in EVERY consumer (round-4 F2)
        bad_reg = root / "bad-reg.json"
        bad_reg.write_text(json.dumps({
            "schema_version": 1, "milestone_id": "demo-m1", "critique_files": [],
            "generated_by": "t", "generated_at": "t", "updated_at": "t",
            "findings": [{"id": "C1", "severity": "CRITICAL", "critic": "a",
                          "file": "x", "line": 1, "title": "t",
                          "regression_guard": None, "critique_file": "f",
                          "status": "opened", "resolution": None, "history": []}],
        }), encoding="utf-8")
        rc, out, err = run_main(["gate", "demo-m1", "--register", str(bad_reg)])
        expect("gate: hand-edited status refuses, never fail-open (F2)",
               rc == 1 and "opened" in err and "fail-open" in err, f"rc={rc} {err[:120]}")
        rc, out, err = run_main(["summary", "demo-m1", "--register", str(bad_reg)])
        expect("summary: malformed register refuses (F2)", rc == 1, f"rc={rc}")
        rc, out, err = run_main(["set", "--register", str(bad_reg), "demo-m1", "C1",
                                 "fixed", "--resolution", "x"])
        expect("set: malformed register refuses (F2)", rc == 1, f"rc={rc}")

        # 29. milestone-id containment (round-4 F10)
        rc, out, err = run_main(["gate", "../evil", "--repo-root", str(root)])
        expect("gate: path-traversal id refused (F10)",
               rc == 1 and "invalid milestone id" in err, f"rc={rc} {err[:120]}")

        # 30. missing remediation body field refused (round-4 F9)
        nobody = VALID_CRITIQUE.replace("**Proposed fix:** Update it.\n\n", "")
        p.write_text(nobody, encoding="utf-8")
        rc, out, err = run_main(["extract", "--check", str(p)])
        expect("check: missing body field refused (F9)",
               rc == 1 and "Proposed fix" in err and "M1" in err, f"rc={rc} {err[:140]}")

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()

    parser = argparse.ArgumentParser(
        prog="milestone-pipeline-findings.py",
        description="Findings register: extract / set / gate / summary / dedupe",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="parse critique md -> findings.json (or --check lint)")
    p_ext.add_argument("files", nargs="+", metavar="critique.md")
    p_ext.add_argument("--check", action="store_true", help="lint only; no register write")
    p_ext.add_argument("--id", help="milestone id (resolves the register path)")
    p_ext.add_argument("--register", help="explicit findings.json path (overrides --id)")
    p_ext.add_argument("--repo-root", help="repo root override")

    p_set = sub.add_parser("set", help="the ONLY sanctioned status writer")
    p_set.add_argument("id", help="milestone id (path resolution overridden by --register)")
    p_set.add_argument("finding_ids", help="finding id or comma-list (C1,H2)")
    p_set.add_argument("status", choices=sorted(STATUSES - {"open"}))
    p_set.add_argument("--resolution", required=False, help="commit sha / reason / note (REQUIRED)")
    p_set.add_argument("--register", help="explicit findings.json path")
    p_set.add_argument("--repo-root", help="repo root override")

    p_gate = sub.add_parser("gate", help="exit 3 while any CRITICAL/HIGH is open")
    p_gate.add_argument("id", nargs="?", help="milestone id (or use --register)")
    p_gate.add_argument("--register", help="explicit findings.json path")
    p_gate.add_argument("--repo-root", help="repo root override")

    p_sum = sub.add_parser("summary", help="JSON summary (counts + status arrays)")
    p_sum.add_argument("id", nargs="?", help="milestone id (or use --register)")
    p_sum.add_argument("--field", help="print one key of the summary JSON")
    p_sum.add_argument("--counts-for", help="print {critical,high,medium,low} for ONE critique file")
    p_sum.add_argument("--register", help="explicit findings.json path")
    p_sum.add_argument("--repo-root", help="repo root override")

    p_dd = sub.add_parser("dedupe", help="cross-critic agreement callouts (in-place)")
    p_dd.add_argument("file", metavar="critique.md")

    args = parser.parse_args(argv)

    if args.cmd == "extract":
        if not args.check and not (args.id or args.register):
            parser.error("extract: pass --id <ID> (or --register PATH), or use --check for lint-only")
        return cmd_extract(args)
    if args.cmd == "set":
        return cmd_set(args)
    if args.cmd == "gate":
        return cmd_gate(args)
    if args.cmd == "summary":
        return cmd_summary(args)
    if args.cmd == "dedupe":
        return cmd_dedupe(args)
    parser.error("unknown subcommand")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
