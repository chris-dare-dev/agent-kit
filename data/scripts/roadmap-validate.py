#!/usr/bin/env python3
"""Lint a <slug>-roadmap.md file against the canonical schema.

Usage:
  validate-roadmap.py <path-to-roadmap.md> [--allow CHECK[,CHECK...]]

Checks:
  required-sections   All required H2 headings present
  goal-fields         ## Goal section has Objective + KRs + Won't subsections
  must-cap            MoSCoW Musts ≤ 60% of in-scope (non-Won't) epics
  milestone-ids       All Now-lane milestones use `<slug>-mN` ID format
  must-assumptions    No `[MUST]` assumption left unvalidated past Phase 2
  spike-lane          Spike lane present (even if "all assumptions validated")
  story-ac            Every Now-lane story has Given/When/Then AC
  status-checkbox     Every milestone heading carries a machine-readable status checkbox
  cross-references    Cross-references section present and non-empty

Exit code 0 if all checks pass; 1 if any check fails (warnings printed).
Use --allow to suppress specific checks (e.g. --allow must-cap).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REQUIRED_SECTIONS = [
    "## Goal",
    "## Epics",
    "## Roadmap — Now / Next / Later",
    "## Cross-references",
]

GOAL_REQUIRED_KEYWORDS = ["**Objective:**", "**Key Results:**", "**Won't"]


def check_required_sections(text: str) -> list[str]:
    failures = []
    for sec in REQUIRED_SECTIONS:
        if sec not in text:
            failures.append(f"required-sections: missing '{sec}'")
    return failures


def check_goal_fields(text: str) -> list[str]:
    failures = []
    goal_match = re.search(r"^## Goal\b(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    if not goal_match:
        return ["goal-fields: ## Goal section not found"]
    goal_body = goal_match.group(1)
    for kw in GOAL_REQUIRED_KEYWORDS:
        if kw not in goal_body:
            failures.append(f"goal-fields: ## Goal missing '{kw}'")
    return failures


def extract_slug(text: str) -> str | None:
    m = re.search(r"^>\s*Slug:\s*`([a-z0-9][a-z0-9-]*)`", text, re.MULTILINE)
    return m.group(1) if m else None


def check_milestone_ids(text: str) -> list[str]:
    failures = []
    slug = extract_slug(text)
    if not slug:
        return ["milestone-ids: cannot determine slug from header (need '> Slug: `<slug>`')"]
    # Find Now lane milestones — e.g. "milestone ID `cost-visibility-l3-m1`"
    pattern = re.compile(r"milestone ID `([^`]+)`")
    found = pattern.findall(text)
    if not found:
        return failures  # No milestones declared yet (Phase 3 not run); not a failure here
    expected_re = re.compile(rf"^{re.escape(slug)}-m\d+$")
    for mid in found:
        if not expected_re.match(mid):
            failures.append(
                f"milestone-ids: '{mid}' does not match expected format '{slug}-m<N>'"
            )
    return failures


def check_must_assumptions(text: str) -> list[str]:
    failures = []
    # Look for `[MUST]` lines that don't include "Validation:" with a non-TODO content.
    # Heuristic: if a `[MUST]` line says "Validation: TODO" or "Validation: <!--", flag it.
    must_lines = re.findall(r"^.*`\[MUST\]`.*$", text, re.MULTILINE)
    for line in must_lines:
        if "Validation:" not in line:
            failures.append(f"must-assumptions: missing 'Validation:' clause on line: {line.strip()[:100]}")
        elif re.search(r"Validation:\s*(TODO|<!--|FIXME|<placeholder>)", line, re.IGNORECASE):
            failures.append(f"must-assumptions: unvalidated [MUST]: {line.strip()[:100]}")
    return failures


def check_spike_lane(text: str) -> list[str]:
    if "### Spike / discovery lane" not in text and "## Spike" not in text:
        return ["spike-lane: missing '### Spike / discovery lane' subsection (required even if empty)"]
    return []


def check_status_checkbox(text: str) -> list[str]:
    """Every milestone heading must be followed by a machine-readable status checkbox
    (`- [ ] **Status:**` / `- [/]` / `- [x]`) — the live status boards and
    pipeline-reconcile.py read it (workspace convention: Roadmap milestone status)."""
    failures = []
    for m in re.finditer(r"^####\s+.*milestone ID `([^`]+)`\s*$", text, re.MULTILINE):
        tail = text[m.end():]
        nxt = re.search(r"^#{1,4}\s", tail, re.MULTILINE)
        block = tail[: nxt.start()] if nxt else tail
        if not re.search(r"^\s*-\s*\[[ x/]\]\s*\*\*Status", block, re.MULTILINE):
            failures.append(
                f"status-checkbox: milestone '{m.group(1)}' has no '- [ ] **Status:**' line"
            )
    return failures


def check_story_ac(text: str) -> list[str]:
    """For each Now-lane story (`**S<n>.<m>:`), require at least one Given/When/Then line."""
    failures = []
    # Find the Now lane block
    now_match = re.search(
        r"^### Now\b(.*?)(?=^### Next|^### Later|^## )",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not now_match:
        return failures  # Phase 3 not run yet
    now_body = now_match.group(1)
    # Find all stories
    stories = re.findall(
        r"\*\*(S\d+\.\d+):\*\*\s*([^\n]+)\n((?:.(?!\*\*S\d+\.\d+:))*)",
        now_body,
        re.DOTALL,
    )
    for story_id, title, body in stories:
        # Check at least one Given/When/Then in the body
        if not re.search(r"\bGiven\b.*\bWhen\b.*\bThen\b", body, re.IGNORECASE | re.DOTALL):
            failures.append(
                f"story-ac: {story_id} ('{title.strip()[:60]}') has no Given/When/Then AC"
            )
    return failures


def check_must_cap(text: str) -> list[str]:
    """Heuristic: count epics in Now (Must) + Next/Later by lane and warn if > 60% are in Now/Must."""
    # This is best-effort; the source-of-truth check is via score-moscow.py on a JSON input.
    failures = []
    # Try to count epic mentions per lane.
    now_count = len(re.findall(r"^####\s+M\d+:", text, re.MULTILINE))
    next_section = re.search(r"^### Next\b(.*?)(?=^### Later|^### Spike|^### Won)", text, re.MULTILINE | re.DOTALL)
    later_section = re.search(r"^### Later\b(.*?)(?=^### Spike|^### Won|^## )", text, re.MULTILINE | re.DOTALL)
    next_count = len(re.findall(r"^- \*\*E\d+:", next_section.group(1), re.MULTILINE)) if next_section else 0
    later_count = len(re.findall(r"^- \*\*E\d+:", later_section.group(1), re.MULTILINE)) if later_section else 0
    in_scope = now_count + next_count + later_count
    if in_scope == 0:
        return failures
    pct = now_count / in_scope
    if pct > 0.60:
        failures.append(
            f"must-cap (heuristic): Now-lane {now_count}/{in_scope} = {pct*100:.1f}% > 60%. "
            "Re-bucket some epics into Next/Later. (Use score-moscow.py on JSON for authoritative check.)"
        )
    return failures


def check_cross_references(text: str) -> list[str]:
    failures = []
    cr_match = re.search(r"^## Cross-references\b(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not cr_match:
        return ["cross-references: section missing"]
    body = cr_match.group(1).strip()
    if len(body) < 50:
        failures.append("cross-references: section is empty or near-empty (< 50 chars)")
    if "/milestone-pipeline" not in body:
        failures.append("cross-references: missing /milestone-pipeline invocation lines")
    return failures


CHECKS = {
    "required-sections": check_required_sections,
    "goal-fields": check_goal_fields,
    "milestone-ids": check_milestone_ids,
    "must-assumptions": check_must_assumptions,
    "spike-lane": check_spike_lane,
    "story-ac": check_story_ac,
    "status-checkbox": check_status_checkbox,
    "must-cap": check_must_cap,
    "cross-references": check_cross_references,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    allowed = set()
    for arg in argv[2:]:
        if arg.startswith("--allow"):
            if "=" in arg:
                allowed.update(arg.split("=", 1)[1].split(","))
            elif argv.index(arg) + 1 < len(argv):
                allowed.update(argv[argv.index(arg) + 1].split(","))

    text = path.read_text()

    all_failures: list[tuple[str, str]] = []
    for name, fn in CHECKS.items():
        if name in allowed:
            continue
        for f in fn(text):
            all_failures.append((name, f))

    if not all_failures:
        print(f"OK: {path}")
        if allowed:
            print(f"  (suppressed checks: {', '.join(sorted(allowed))})")
        return 0

    print(f"FAIL: {path}")
    print(f"  {len(all_failures)} issue(s):")
    for name, msg in all_failures:
        print(f"  - [{name}] {msg}")
    if allowed:
        print(f"  (suppressed: {', '.join(sorted(allowed))})")
    print()
    print("To suppress a check (only after intentional acceptance):")
    print(f"  validate-roadmap.py {path} --allow <check-name>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
