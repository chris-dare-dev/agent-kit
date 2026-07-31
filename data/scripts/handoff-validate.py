#!/usr/bin/env python3
"""Validate session-handoff documents against the handoff contract, and insert
parser-safe review checkpoints into roadmap docs.

Contract: data/references/handoff-contract.md (filename grammar, frontmatter
schema, body sections, roadmap review-checkpoint format).

Modes:
  --file PATH [--strict]     Validate one handoff document. Exit 0 = pass
                             (warnings allowed unless --strict), 1 = failures.
  --claim-name NAME [--map PATH]
                             Report which project-map.json project(s) claim a
                             candidate handoff filename (pre-flight naming).
  --insert-checkpoint --roadmap PATH --handoff REL --covers a,b --reviewer X [--date D]
                             Idempotently append the optional review-audit task
                             to the roadmap's `### Review checkpoints` section.
  --self-test                Run embedded fixtures; exit 0 iff all pass.

Stdlib-only. The frontmatter parser is deliberately minimal (top-level
`key: value` scalars + `key:` block lists) — the schema it validates uses
nothing else.
"""

from __future__ import annotations

import os
import re
import sys
import json
import argparse
import tempfile
from datetime import date as _date  # noqa: F401  (kept for callers; not used for defaults)

FILENAME_RE = re.compile(
    r"^HANDOFF-(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]*?)-(continuation|session-review)\.md$"
)
SUFFIX_KIND = {"continuation": "continuation", "session-review": "review"}
CHECKPOINT_HEADING = "### Review checkpoints"
CHECKPOINT_COMMENT = (
    "<!-- Optional external-audit tasks appended by /handoff review (handoff-contract.md §6)."
    " Keep lines starting '- [ ] (optional) session audit' — parser-safe, not milestones. -->"
)
REVISION_HEADING_RE = re.compile(r"^##\s+Revision[ -]history\b", re.I)

REQUIRED_KEYS_ALL = ["type", "handoff_kind", "project", "date", "status"]
REQUIRED_KEYS_REVIEW = ["reviewer_target", "review_status", "roadmap", "milestones_covered"]

SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret)\b\s*[:=]\s*([^\s<$`{(][^\s]{6,})"
)


# ---------------------------------------------------------------------------
# frontmatter (minimal, text-level — mirrors the vault stampers' approach)
# ---------------------------------------------------------------------------

def parse_frontmatter(text):
    """Return (dict, err). Lists are Python lists; scalars are strings."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, "no frontmatter block (file must start with ---)"
    fm, i, key = {}, 1, None
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "---":
            return fm, None
        if re.match(r"^\s*#", ln) or not ln.strip():
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", ln)
        if m:
            key, val = m.group(1), m.group(2).strip()
            val = re.sub(r"\s+#.*$", "", val).strip()  # trailing comment
            if val == "":
                fm[key] = []  # block list (or empty)
            else:
                fm[key] = val.strip("\"'")
        elif re.match(r"^\s+-\s+", ln) and key is not None and isinstance(fm.get(key), list):
            fm[key].append(re.sub(r"^\s+-\s+", "", ln).strip().strip("\"'"))
        i += 1
    return None, "unterminated frontmatter block"


# ---------------------------------------------------------------------------
# vault discovery + project claiming (replicates project-linker.py matches())
# ---------------------------------------------------------------------------

def find_vault_root(start=None):
    """Walk up from `start` looking for the workspace/vault root; fall back to
    $PERSONAL_WORKSPACE_ROOT, then ~/Work/workspace. Returns a path or None."""
    def looks_like_root(d):
        return (os.path.isfile(os.path.join(d, "scripts", "project-map.json"))
                or (os.path.isdir(os.path.join(d, ".obsidian")) and os.path.isdir(os.path.join(d, "plans"))))
    if start:
        d = os.path.abspath(start)
        while True:
            if looks_like_root(d):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    env = os.environ.get("PERSONAL_WORKSPACE_ROOT")
    if env and looks_like_root(env):
        return env
    home_default = os.path.expanduser("~/Work/workspace")
    if looks_like_root(home_default):
        return home_default
    return None


def load_project_map(map_path):
    with open(map_path, encoding="utf-8") as f:
        return json.load(f)


def matches(filename, cfg):
    """Byte-compatible with project-linker.py matches(): excludes win; then
    slug-prefix, `contains` substring, or hyphen/underscore-bounded slug segment."""
    low = filename.lower()
    if any(x.lower() in low for x in cfg.get("excludes", [])):
        return False
    if any(low.startswith(s.lower()) for s in cfg.get("slugs", [])):
        return True
    if any(c.lower() in low for c in cfg.get("contains", [])):
        return True
    for s in cfg.get("slugs", []):
        sl = s.lower().rstrip("-.")
        if len(sl) >= 4 and re.search(r"(?:^|[-_])" + re.escape(sl) + r"(?:[-_.]|$)", low):
            return True
    return False


def claiming_projects(filename, project_map):
    return [name for name, cfg in project_map.get("projects", {}).items() if matches(filename, cfg)]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.failures, self.warnings = [], []

    def fail(self, msg):
        self.failures.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def emit(self, label, strict=False):
        for w in self.warnings:
            print(f"WARN  {label}: {w}")
        for f in self.failures:
            print(f"FAIL  {label}: {f}")
        bad = bool(self.failures) or (strict and bool(self.warnings))
        print(f"{'FAIL' if bad else 'PASS'}  {label} "
              f"({len(self.failures)} failure(s), {len(self.warnings)} warning(s))")
        return not bad


def resolve_roadmap(roadmap_val, handoff_path, vault_root):
    """Resolve a frontmatter `roadmap:` value to an existing file, or None."""
    cands = []
    if os.path.isabs(roadmap_val):
        cands.append(roadmap_val)
    else:
        if vault_root:
            cands.append(os.path.join(vault_root, roadmap_val))
        cands.append(os.path.join(os.path.dirname(os.path.abspath(handoff_path)), roadmap_val))
        cands.append(roadmap_val)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def validate_file(path, strict=False, map_override=None):
    rep = Report()
    base = os.path.basename(path)
    if not os.path.isfile(path):
        rep.fail(f"file not found: {path}")
        return rep.emit(base, strict)
    text = open(path, encoding="utf-8", errors="replace").read()

    # F1 filename grammar
    m = FILENAME_RE.match(base)
    fname_date, fname_kind = None, None
    if not m:
        rep.fail("filename must match HANDOFF-<YYYY-MM-DD>-<project-slug>[-<detail>]-"
                 "(continuation|session-review).md")
    else:
        fname_date, fname_kind = m.group(1), SUFFIX_KIND[m.group(3)]

    # F2 frontmatter parses
    fm, err = parse_frontmatter(text)
    if fm is None:
        rep.fail(f"frontmatter: {err}")
        return rep.emit(base, strict)

    # F3 required keys + values
    for k in REQUIRED_KEYS_ALL:
        if k not in fm or fm[k] in ("", []):
            rep.fail(f"frontmatter: missing required key '{k}'")
    tval = fm.get("type")
    if tval and tval != "handoff":
        rep.fail(f"frontmatter: type must be exactly 'handoff' (got '{tval}') — "
                 "legacy values like 'session-handoff' are invisible to Handoffs.base")
    kind = fm.get("handoff_kind")
    if kind and kind not in ("continuation", "review"):
        rep.fail(f"frontmatter: handoff_kind must be continuation|review (got '{kind}')")
    if kind and fname_kind and kind != fname_kind:
        rep.fail(f"frontmatter: handoff_kind '{kind}' does not match filename suffix "
                 f"('{fname_kind}' expected)")
    if "handoff-kind" in fm:
        rep.fail("frontmatter: use 'handoff_kind' (underscore), not 'handoff-kind'")
    proj = fm.get("project", "")
    if proj and not re.match(r"^[a-z0-9][a-z0-9-]*$", str(proj)):
        rep.warn(f"project '{proj}' is not a lowercase-kebab slug")
    if fname_date and fm.get("date") and fm["date"] != fname_date:
        rep.fail(f"frontmatter: date '{fm['date']}' != filename date '{fname_date}'")
    if fm.get("authorship") != "agent-generated":
        rep.warn("frontmatter: authorship should be 'agent-generated'")
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    if "type/handoff" not in tags:
        rep.warn("tags: missing 'type/handoff'")
    if kind and f"handoff/{kind}" not in tags:
        rep.warn(f"tags: missing 'handoff/{kind}'")

    comp = fm.get("companion")
    if not comp:
        rep.warn("frontmatter: no 'companion' — a lone handoff is a smell "
                 "(review without continuation, or vice versa)")
    elif isinstance(comp, str) and not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(path)), comp)):
        rep.warn(f"companion '{comp}' not found next to this file (write it before session end)")

    vault_root = find_vault_root(os.path.dirname(os.path.abspath(path)))

    if kind == "review":
        for k in REQUIRED_KEYS_REVIEW:
            if k not in fm or fm[k] in ("", []):
                rep.fail(f"frontmatter: review handoffs require '{k}'")
        mc = fm.get("milestones_covered")
        if isinstance(mc, str):
            rep.fail("frontmatter: milestones_covered must be a YAML list")
        rm = fm.get("roadmap")
        if isinstance(rm, str) and rm:
            resolved = resolve_roadmap(rm, path, vault_root)
            if not resolved:
                rep.fail(f"roadmap '{rm}' not found (tried vault-root-relative, "
                         "file-relative, as-given)")
            elif base not in open(resolved, encoding="utf-8", errors="replace").read():
                rep.warn(f"roadmap has no review checkpoint naming this handoff — run "
                         f"--insert-checkpoint (roadmap: {resolved})")

    # F4 body sections
    body = text.split("---", 2)[-1]
    if not re.search(r"^#\s.*HANDOFF", body, re.M | re.I):
        rep.warn("body: H1 should name the handoff (e.g. '# CONTINUATION HANDOFF — …')")
    if kind == "continuation":
        if not re.search(r"RESUME\s+HERE", body, re.I):
            rep.fail("body: continuation handoffs need a 'RESUME HERE' section "
                     "(the exact next step)")
        if not re.search(r"^#{1,6}\s.*current state", body, re.M | re.I):
            rep.warn("body: no 'Current state' section")
    if kind == "review":
        if not re.search(r"^#{1,6}\s.*TL;DR", body, re.M):
            rep.fail("body: review handoffs need a TL;DR section (work table)")
        if not re.search(r"SCRUTIN", body, re.I):
            rep.fail("body: review handoffs need 'What to SCRUTINIZE' guidance "
                     "(per work item)")
        if not re.search(r"^#{1,6}\s.*verification evidence", body, re.M | re.I):
            rep.warn("body: no 'Verification evidence' section")

    # F5 project claim-check
    map_path = map_override or (os.path.join(vault_root, "scripts", "project-map.json") if vault_root else None)
    if map_path and os.path.isfile(map_path):
        claims = claiming_projects(base, load_project_map(map_path))
        if not claims:
            rep.fail("filename is claimed by NO project in project-map.json — it will never "
                     "surface in a hub or Home Workstreams. Rename to a claimed slug, or add a "
                     "'contains' token to the owning project.")
        else:
            print(f"INFO  {base}: claimed by {', '.join(claims)}")
    else:
        rep.warn("project-map.json not found — claim-check skipped (no vault tooling here?)")

    # F6 secret sniff
    for i, ln in enumerate(body.split("\n"), 1):
        sm = SECRET_RE.search(ln)
        if sm and "secretsmanager" not in ln.lower() and "secret-id" not in ln.lower():
            rep.warn(f"possible literal secret at body line {i}: '{ln.strip()[:60]}…' — "
                     "reference SM paths/env vars, never values")

    return rep.emit(base, strict)


# ---------------------------------------------------------------------------
# review-checkpoint insertion (idempotent, parser-safe)
# ---------------------------------------------------------------------------

def checkpoint_line(dt, covers, handoff_rel, reviewer):
    ids = ", ".join(f"`{c.strip()}`" for c in covers.split(",") if c.strip())
    return (f"- [ ] (optional) session audit {dt} — covers {ids} · "
            f"handoff: `{handoff_rel}` · reviewer: {reviewer}")


def insert_checkpoint(roadmap_path, handoff_rel, covers, reviewer, dt=None):
    vault_root = find_vault_root(os.getcwd())
    path = roadmap_path
    if not os.path.isfile(path) and not os.path.isabs(path) and vault_root:
        cand = os.path.join(vault_root, roadmap_path)
        if os.path.isfile(cand):
            path = cand
    if not os.path.isfile(path):
        print(f"FAIL  roadmap not found: {roadmap_path}", file=sys.stderr)
        return 1
    base = os.path.basename(handoff_rel)
    if dt is None:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", base)
        if not m:
            print("FAIL  --date required (handoff filename carries no date)", file=sys.stderr)
            return 1
        dt = m.group(1)
    text = open(path, encoding="utf-8").read()
    if base in text:
        print(f"OK    checkpoint already present in {path} (idempotent no-op)")
        return 0
    line = checkpoint_line(dt, covers, handoff_rel, reviewer)
    lines = text.split("\n")
    if CHECKPOINT_HEADING in text:
        # append right after the existing section's last checkpoint line
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == CHECKPOINT_HEADING)
        j = idx + 1
        while j < len(lines) and not re.match(r"^#{1,6}\s", lines[j]):
            j += 1
        # back up over trailing blanks so the new line joins the list
        k = j
        while k > idx + 1 and lines[k - 1].strip() == "":
            k -= 1
        lines[k:k] = [line]
    else:
        block = ["", CHECKPOINT_HEADING, "", CHECKPOINT_COMMENT, "", line]
        rev = next((i for i, ln in enumerate(lines) if REVISION_HEADING_RE.match(ln)), None)
        if rev is not None:
            # place before ## Revision history (and before its preceding '---' rule, if any)
            ins = rev
            if ins > 0 and lines[ins - 1].strip() == "---":
                ins -= 1
            lines[ins:ins] = block + [""]
        else:
            lines.extend(block)
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print(f"OK    inserted into {path}:\n      {line}")
    return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

FIXTURE_MAP = {
    "projects": {
        "Test Project": {"project_id": "testproj", "slugs": ["testproj"], "contains": ["tp-sub"]},
        "Other": {"project_id": "other", "slugs": ["otherproj"], "excludes": ["testproj-not-mine"]},
    }
}

FIXTURE_CONT = """---
authorship: agent-generated
type: handoff
handoff_kind: continuation
project: testproj
date: 2026-07-12
status: complete
companion: HANDOFF-2026-07-12-testproj-session-review.md
roadmap: plans/testproj-roadmap.md
tags:
  - type/handoff
  - project/testproj
  - handoff/continuation
  - authorship/agent-generated
---

# CONTINUATION HANDOFF — testproj (2026-07-12)

## 1. Current state (as of this handoff)
| Phase | Status |
|---|---|
| m1 | done |

## 2. RESUME HERE — next step
Do the thing.
"""

FIXTURE_REVIEW = """---
authorship: agent-generated
type: handoff
handoff_kind: review
project: testproj
date: 2026-07-12
status: complete
review_status: requested
reviewer_target: gpt-5.6-sol-ultra
companion: HANDOFF-2026-07-12-testproj-continuation.md
roadmap: plans/testproj-roadmap.md
milestones_covered:
  - testproj-m1
  - testproj-m2
tags:
  - type/handoff
  - project/testproj
  - handoff/review
  - review/requested
  - authorship/agent-generated
---

# HANDOFF (REVIEW) — testproj session, 2026-07-12

## 0. TL;DR — what this session did
| # | Work | Repos | SHAs | State |
|---|---|---|---|---|
| 1 | m1 | r | abc | SHIPPED |

## 1. Milestone 1
### What to SCRUTINIZE
Break this claim.

## 2. Verification evidence (as of handoff)
Tests green.
"""

FIXTURE_ROADMAP = """# Roadmap: testproj

## Goal
x

## Cross-references

- some link

---

## Revision history

- 2026-07-12 — created.
"""


def self_test():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"{'ok' if cond else 'FAIL'}  self-test: {name}")
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"))
        os.makedirs(os.path.join(td, "plans"))
        os.makedirs(os.path.join(td, ".obsidian"))
        with open(os.path.join(td, "scripts", "project-map.json"), "w", encoding="utf-8") as f:
            json.dump(FIXTURE_MAP, f)
        rp = os.path.join(td, "plans", "testproj-roadmap.md")
        open(rp, "w", encoding="utf-8").write(FIXTURE_ROADMAP)

        cont = os.path.join(td, "plans", "HANDOFF-2026-07-12-testproj-continuation.md")
        open(cont, "w", encoding="utf-8").write(FIXTURE_CONT)
        rev = os.path.join(td, "plans", "HANDOFF-2026-07-12-testproj-session-review.md")
        open(rev, "w", encoding="utf-8").write(FIXTURE_REVIEW)

        # checkpoint insertion (before validation so the review's roadmap warn clears)
        rc = insert_checkpoint(rp, "plans/HANDOFF-2026-07-12-testproj-session-review.md",
                               "testproj-m1,testproj-m2", "gpt-5.6-sol-ultra")
        check("insert-checkpoint returns 0", rc == 0)
        txt = open(rp, encoding="utf-8").read()
        check("checkpoint section created", CHECKPOINT_HEADING in txt)
        check("checkpoint line parser-safe prefix",
              "- [ ] (optional) session audit 2026-07-12" in txt)
        check("checkpoint precedes revision history",
              txt.index(CHECKPOINT_HEADING) < txt.index("## Revision history"))
        rc2 = insert_checkpoint(rp, "plans/HANDOFF-2026-07-12-testproj-session-review.md",
                                "testproj-m1", "gpt-5.6-sol-ultra")
        check("insert is idempotent", rc2 == 0 and
              open(rp, encoding="utf-8").read().count("HANDOFF-2026-07-12-testproj-session-review.md") == 1)

        check("valid continuation passes", validate_file(cont) is True)
        check("valid review passes", validate_file(rev) is True)

        # failure cases
        badtype = os.path.join(td, "plans", "HANDOFF-2026-07-12-testproj-x-continuation.md")
        open(badtype, "w", encoding="utf-8").write(FIXTURE_CONT.replace("type: handoff", "type: session-handoff"))
        check("legacy type fails", validate_file(badtype) is False)

        mismatch = os.path.join(td, "plans", "HANDOFF-2026-07-12-testproj-y-session-review.md")
        open(mismatch, "w", encoding="utf-8").write(FIXTURE_CONT)  # continuation fm under review suffix
        check("kind/suffix mismatch fails", validate_file(mismatch) is False)

        unclaimed = os.path.join(td, "plans", "HANDOFF-2026-07-12-zzunknown-continuation.md")
        open(unclaimed, "w", encoding="utf-8").write(FIXTURE_CONT.replace("project: testproj", "project: zzunknown"))
        check("unclaimed filename fails", validate_file(unclaimed) is False)

        claims = claiming_projects("HANDOFF-2026-07-12-tp-sub-foo-continuation.md",
                                   FIXTURE_MAP)
        check("contains-token claims", claims == ["Test Project"])
        check("excludes win", claiming_projects(
            "HANDOFF-2026-07-12-testproj-not-mine-continuation.md", FIXTURE_MAP) == ["Test Project"])

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="validate one handoff document")
    ap.add_argument("--strict", action="store_true", help="warnings fail too")
    ap.add_argument("--claim-name", help="report claiming project(s) for a candidate filename")
    ap.add_argument("--map", help="explicit project-map.json path")
    ap.add_argument("--insert-checkpoint", action="store_true")
    ap.add_argument("--roadmap", help="roadmap path (abs, cwd-rel, or vault-root-rel)")
    ap.add_argument("--handoff", help="handoff path as it should appear in the roadmap line")
    ap.add_argument("--covers", help="comma-separated milestone ids covered")
    ap.add_argument("--reviewer", default="gpt-5.6-sol-ultra")
    ap.add_argument("--date", help="override the audit date (default: from handoff filename)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    if args.claim_name:
        mp = args.map
        if not mp:
            vr = find_vault_root(os.getcwd())
            mp = os.path.join(vr, "scripts", "project-map.json") if vr else None
        if not mp or not os.path.isfile(mp):
            print("FAIL  no project-map.json found (pass --map)", file=sys.stderr)
            sys.exit(1)
        claims = claiming_projects(args.claim_name, load_project_map(mp))
        if claims:
            print(f"CLAIMED by: {', '.join(claims)}")
            sys.exit(0)
        print("UNCLAIMED — rename to a claimed slug or add a 'contains' token to the owning "
              "project in project-map.json")
        sys.exit(1)
    if args.insert_checkpoint:
        if not (args.roadmap and args.handoff and args.covers):
            ap.error("--insert-checkpoint requires --roadmap, --handoff, --covers")
        sys.exit(insert_checkpoint(args.roadmap, args.handoff, args.covers,
                                   args.reviewer, args.date))
    if args.file:
        sys.exit(0 if validate_file(args.file, strict=args.strict, map_override=args.map) else 1)
    ap.error("one of --file / --claim-name / --insert-checkpoint / --self-test is required")


if __name__ == "__main__":
    main()
