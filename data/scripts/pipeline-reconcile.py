#!/usr/bin/env python3
"""Reconcile pipeline artifacts: register <-> roadmap doc <-> state.json <-> critiques.

Usage:
  pipeline-reconcile.py [--repo-root PATH] [--slug SLUG]
  pipeline-reconcile.py --self-test

For every .claude/notes/roadmaps/<slug>/milestones.json under the repo root (or just
<slug> if given):

  doc-exists        The declared roadmap_doc file exists.
  id-sync           Every milestone id in the register appears in the roadmap doc
                    (as `milestone ID \\`<id>\\``) and vice versa.
  checkbox-sync     The roadmap doc's status checkbox under each milestone heading
                    matches the register status (- [ ] pending | - [/] in_progress |
                    - [x] complete; workspace convention). A 'cancelled' milestone
                    must not show a ticked box.
  state-sync        If run.state_path is set: the state.json exists; phase 'complete'
                    implies status 'complete' and vice versa.
  critique-sync     If the state.json passed critique-complete: critique_path is set
                    and exists, EVERY entry of critique_files exists, and the adversary
                    file's 'Severity counts: C_ H_ M_ L_' header matches
                    state.critique_finding_counts.
  findings-sync     For every .claude/notes/milestones/<id>/findings.json (the Phase 3
                    findings register — scanned directly, so ad-hoc runs without a
                    roadmap register are covered too): every critique_files entry
                    exists; the register's per-file severity tallies match each file's
                    'Severity counts:' line (prefix-aware: F-C0 / I-C1 forms); the
                    adjacent state.json agrees — at phase 'complete' there must be no
                    open CRITICAL/HIGH finding (the gate's advisory mirror) and
                    fixed_findings/deferred_findings/invalidated_findings must equal
                    the register-derived id sets.

This is the drift-catcher for the prod-sync-m2 failure class (state.json reached
'complete' with null critique fields while the critique file existed on disk).

All artifacts checked here are LOCAL-ONLY (never committed — policy 2026-07-09), so
this runs MACHINE-SIDE at pipeline terminal transitions; CI runs only --self-test.
Humans decide what to do with findings.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CHECKBOX_TO_STATUS = {"[ ]": "pending", "[/]": "in_progress", "[x]": "complete"}
STATUS_TO_CHECKBOX = {v: k for k, v in CHECKBOX_TO_STATUS.items()}

# Phases at-or-beyond which the critique artifacts must exist.
POST_CRITIQUE_PHASES = {"critique-complete", "rectify-running", "complete"}

MILESTONE_ID_RE = re.compile(r"milestone ID `([^`]+)`")
# Prefix-aware: conditional critics emit 'Severity counts: F-C0 F-H1 …' /
# 'I-C1 …'; the adversary emits the plain form. One regex serves critique-sync
# and findings-sync.
SEVERITY_RE = re.compile(
    r"Severity counts:\*{0,2}\s*(?:[A-Z]-)?C(\d+)\s+(?:[A-Z]-)?H(\d+)"
    r"\s+(?:[A-Z]-)?M(\d+)\s+(?:[A-Z]-)?L(\d+)"
)


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
    sys.exit("could not determine repo root. Pass --repo-root or set REPO_ROOT.")


def _checkbox_for(doc_text: str, mid: str) -> str | None:
    """Find the status checkbox on the line(s) following this milestone's heading."""
    heading = re.search(
        rf"^####\s+.*milestone ID `{re.escape(mid)}`\s*$", doc_text, re.MULTILINE
    )
    if not heading:
        return None
    tail = doc_text[heading.end():]
    next_heading = re.search(r"^#{1,4}\s", tail, re.MULTILINE)
    block = tail[: next_heading.start()] if next_heading else tail
    box = re.search(r"^\s*-\s*(\[[ x/]\])\s*\*\*Status", block, re.MULTILINE)
    return box.group(1) if box else None


def reconcile_file(mfile: Path, repo_root: Path) -> list[str]:
    findings: list[str] = []
    rel = mfile.relative_to(repo_root)

    try:
        doc = json.loads(mfile.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"{rel}: unreadable milestones file: {exc}"]
    milestones = [m for m in doc.get("milestones", []) if isinstance(m, dict)]

    # doc-exists
    roadmap_rel = doc.get("roadmap_doc", "")
    roadmap_path = repo_root / roadmap_rel
    if not roadmap_path.is_file():
        findings.append(f"{rel}: [doc-exists] roadmap_doc '{roadmap_rel}' not found")
        roadmap_text = None
    else:
        roadmap_text = roadmap_path.read_text(encoding="utf-8")

    # id-sync + checkbox-sync
    if roadmap_text is not None:
        doc_ids = set(MILESTONE_ID_RE.findall(roadmap_text))
        json_ids = {m.get("id") for m in milestones}
        for mid in sorted(json_ids - doc_ids):
            findings.append(f"{rel}: [id-sync] '{mid}' in JSON but not in {roadmap_rel}")
        for mid in sorted(doc_ids - json_ids):
            findings.append(f"{rel}: [id-sync] '{mid}' in {roadmap_rel} but not in JSON")
        for m in milestones:
            mid, status = m.get("id"), m.get("status")
            box = _checkbox_for(roadmap_text, mid) if mid in doc_ids else None
            if box is None:
                continue  # id-sync already covers absence; no status line is common pre-Phase-3
            if status == "cancelled":
                if box == "[x]":
                    findings.append(
                        f"{rel}: [checkbox-sync] {mid} is cancelled but doc shows ticked '[x]'"
                    )
            elif CHECKBOX_TO_STATUS.get(box) != status:
                findings.append(
                    f"{rel}: [checkbox-sync] {mid} doc checkbox '{box}' != status "
                    f"'{status}' (expected '{STATUS_TO_CHECKBOX.get(status, '?')}')"
                )

    # state-sync + critique-sync
    for m in milestones:
        mid = m.get("id", "?")
        state_rel = (m.get("run") or {}).get("state_path")
        if not state_rel:
            continue
        state_path = repo_root / state_rel
        if not state_path.is_file():
            findings.append(f"{rel}: [state-sync] {mid}: state_path '{state_rel}' not found")
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            findings.append(f"{rel}: [state-sync] {mid}: unreadable state.json: {exc}")
            continue
        phase, status = state.get("phase"), m.get("status")
        if phase == "complete" and status != "complete":
            findings.append(
                f"{rel}: [state-sync] {mid}: state phase 'complete' but status '{status}'"
            )
        if status == "complete" and phase != "complete":
            findings.append(
                f"{rel}: [state-sync] {mid}: status 'complete' but state phase '{phase}'"
            )
        findings.extend(f"{rel}: {f}" for f in check_critique(state, mid, repo_root))

    return findings


def check_critique(state: dict, mid: str, repo_root: Path) -> list[str]:
    """critique-sync for one state.json (also reusable for ad-hoc milestones)."""
    findings: list[str] = []
    phases_seen = {h.get("phase") for h in state.get("phase_history", [])}
    if not (phases_seen & POST_CRITIQUE_PHASES):
        return findings

    critique_rel = state.get("critique_path")
    if not critique_rel:
        findings.append(f"[critique-sync] {mid}: past critique-complete but critique_path unset")
        return findings
    critique_path = repo_root / critique_rel
    if not critique_path.is_file():
        findings.append(f"[critique-sync] {mid}: critique file '{critique_rel}' not found")
        return findings

    # Every critic that fired must have its output on disk (frontend/infra included).
    for rel in state.get("critique_files") or []:
        if not (repo_root / rel).is_file():
            findings.append(f"[critique-sync] {mid}: critique_files entry '{rel}' not found")

    counts = state.get("critique_finding_counts")
    if not isinstance(counts, dict):
        findings.append(
            f"[critique-sync] {mid}: critique_finding_counts never recorded (null)"
        )
        return findings
    sev = SEVERITY_RE.search(critique_path.read_text(encoding="utf-8"))
    if not sev:
        findings.append(
            f"[critique-sync] {mid}: no 'Severity counts: C_ H_ M_ L_' line in {critique_rel}"
        )
        return findings
    file_counts = dict(zip(("critical", "high", "medium", "low"), map(int, sev.groups())))
    if file_counts != {k: counts.get(k) for k in file_counts}:
        findings.append(
            f"[critique-sync] {mid}: state counts {counts} != critique file counts {file_counts}"
        )
    return findings


def check_findings(state_dir: Path, repo_root: Path) -> list[str]:
    """findings-sync for one milestone state dir (register <-> critiques <-> state)."""
    findings_out: list[str] = []
    reg_path = state_dir / "findings.json"
    mid = state_dir.name
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"[findings-sync] {mid}: unreadable findings.json: {exc}"]
    if reg.get("schema_version") != 1 or not isinstance(reg.get("findings"), list):
        return [f"[findings-sync] {mid}: findings.json is not a v1 findings register"]
    regf = [f for f in reg["findings"] if isinstance(f, dict)]

    # Malformed entries (round-4 F2 mirror — the findings script REFUSES these;
    # reconcile reports them advisorily so drift is visible even when nobody
    # runs the gate). An unknown status must never read as "not open".
    for f in regf:
        if f.get("severity") not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            findings_out.append(
                f"[findings-sync] {mid}: {f.get('id', '?')} has unknown severity "
                f"{f.get('severity')!r}"
            )
        if f.get("status") not in ("open", "fixed", "deferred", "invalidated"):
            findings_out.append(
                f"[findings-sync] {mid}: {f.get('id', '?')} has unknown status "
                f"{f.get('status')!r} (hand-edited register?)"
            )

    # Every critique file exists; its 'Severity counts:' line matches the
    # register's per-file tally.
    for rel in reg.get("critique_files", []):
        cpath = Path(rel) if os.path.isabs(rel) else repo_root / rel
        if not cpath.is_file():
            findings_out.append(
                f"[findings-sync] {mid}: critique_files entry '{rel}' not found"
            )
            continue
        sev = SEVERITY_RE.search(cpath.read_text(encoding="utf-8"))
        if not sev:
            findings_out.append(
                f"[findings-sync] {mid}: no 'Severity counts:' line in {rel}"
            )
            continue
        declared = dict(
            zip(("critical", "high", "medium", "low"), map(int, sev.groups()))
        )
        tally = {k: 0 for k in ("critical", "high", "medium", "low")}
        for f in regf:
            if f.get("critique_file") == rel and isinstance(f.get("severity"), str):
                tally[f["severity"].lower()] += 1
        if declared != tally:
            findings_out.append(
                f"[findings-sync] {mid}: {rel} declares {declared} but the "
                f"register holds {tally} for that file"
            )

    state_path = state_dir / "state.json"
    if not state_path.is_file():
        findings_out.append(
            f"[findings-sync] {mid}: findings.json present but state.json missing"
        )
        return findings_out
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        findings_out.append(f"[findings-sync] {mid}: unreadable state.json: {exc}")
        return findings_out

    if state.get("phase") == "complete":
        open_ch = [
            f["id"] for f in regf
            if f.get("status") == "open" and f.get("severity") in ("CRITICAL", "HIGH")
        ]
        if open_ch:
            findings_out.append(
                f"[findings-sync] {mid}: phase 'complete' but open CRITICAL/HIGH "
                f"finding(s) in the register: {', '.join(open_ch)} (the complete "
                "transition gates on this — state was likely hand-edited)"
            )
        for status, field in (
            ("fixed", "fixed_findings"),
            ("deferred", "deferred_findings"),
            ("invalidated", "invalidated_findings"),
        ):
            reg_ids = {f["id"] for f in regf if f.get("status") == status}
            state_ids = set(state.get(field) or [])
            if reg_ids != state_ids:
                findings_out.append(
                    f"[findings-sync] {mid}: state.{field} {sorted(state_ids)} != "
                    f"register {status} set {sorted(reg_ids)}"
                )
    return findings_out


def run(repo_root: Path, slug: str | None) -> int:
    roadmaps = repo_root / ".claude" / "notes" / "roadmaps"
    pattern = f"{slug}/milestones.json" if slug else "*/milestones.json"
    files = sorted(roadmaps.glob(pattern)) if roadmaps.is_dir() else []

    # findings-sync scans milestone state dirs DIRECTLY (not via the roadmap
    # registers) so ad-hoc runs are covered; slug-filtered runs skip it (the
    # findings register is per-milestone, not per-roadmap).
    milestones_dir = repo_root / ".claude" / "notes" / "milestones"
    findings_regs = (
        sorted(milestones_dir.glob("*/findings.json"))
        if (milestones_dir.is_dir() and not slug)
        else []
    )

    if not files and not findings_regs:
        print(
            f"OK: no registers matching .claude/notes/roadmaps/{pattern} and no "
            "milestone findings registers — nothing to reconcile"
        )
        return 0

    all_findings: list[str] = []
    for mfile in files:
        all_findings.extend(reconcile_file(mfile, repo_root))
    for reg in findings_regs:
        all_findings.extend(
            f"{reg.parent.relative_to(repo_root)}: {f}"
            for f in check_findings(reg.parent, repo_root)
        )

    checked = len(files) + len(findings_regs)
    if not all_findings:
        print(
            f"OK: {len(files)} milestones file(s) + {len(findings_regs)} findings "
            "register(s) reconciled — no drift"
        )
        return 0
    print(f"DRIFT: {len(all_findings)} finding(s) across {checked} file(s):")
    for f in all_findings:
        print(f"  - {f}")
    return 1


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    import tempfile

    failures = 0

    def expect(name: str, findings: list[str], want_substr: str | None) -> None:
        nonlocal failures
        joined = "\n".join(findings)
        ok = (want_substr is None and not findings) or (
            want_substr is not None and want_substr in joined
        )
        print(f"  {name}: {'ok' if ok else f'FAIL (got: {findings})'}")
        if not ok:
            failures += 1

    print("self-test: pipeline-reconcile.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "plans").mkdir()
        (root / "docs").mkdir()
        reg_dir = root / ".claude" / "notes" / "roadmaps" / "demo-slug"
        reg_dir.mkdir(parents=True)
        state_dir = root / ".claude" / "notes" / "milestones" / "demo-slug-m1"
        state_dir.mkdir(parents=True)

        roadmap = root / "plans" / "demo-slug-roadmap.md"
        roadmap.write_text(
            "# Roadmap\n\n"
            "#### M1: Thing one — milestone ID `demo-slug-m1`\n\n"
            "- [x] **Status:** complete\n\n"
            "#### M2: Thing two — milestone ID `demo-slug-m2`\n\n"
            "- [ ] **Status:** pending\n"
        )
        (root / "docs" / "demo-slug-m1_adversary_critique.md").write_text(
            "# Critique\n\nSeverity counts: C0 H1 M2 L0\n"
        )
        state = {
            "id": "demo-slug-m1",
            "phase": "complete",
            "phase_history": [{"phase": p, "at": "t"} for p in (
                "init", "research-running", "research-complete", "implement-running",
                "implement-complete", "critique-running", "critique-complete",
                "rectify-running", "complete")],
            "critique_path": "docs/demo-slug-m1_adversary_critique.md",
            "critique_files": ["docs/demo-slug-m1_adversary_critique.md"],
            "critique_finding_counts": {"critical": 0, "high": 1, "medium": 2, "low": 0},
        }
        (state_dir / "state.json").write_text(json.dumps(state))

        def mkdoc(m1_status: str = "complete", m2_id: str = "demo-slug-m2") -> dict:
            def m(mid: str, status: str, state_path: str | None) -> dict:
                return {
                    "id": mid, "title": "t", "epic": "E1", "lane": "now",
                    "status": status, "depends_on": [], "tags": ["moscow/must"],
                    "rice": 1, "specialist": "general-purpose", "repos": ["r"],
                    "external_writes": [], "gitlab": {"epic_iid": None, "story_iids": []},
                    "run": {"state_path": state_path, "started_at": None,
                            "completed_at": None, "rectification_commit": None,
                            "override": None},
                    "history": [],
                }
            return {
                "schema_version": 1, "slug": "demo-slug",
                "roadmap_doc": "plans/demo-slug-roadmap.md",
                "generated_by": "self-test", "generated_at": "t",
                "milestones": [
                    m("demo-slug-m1", m1_status,
                      ".claude/notes/milestones/demo-slug-m1/state.json"),
                    m(m2_id, "pending", None),
                ],
            }

        mfile = reg_dir / "milestones.json"

        mfile.write_text(json.dumps(mkdoc()))
        expect("clean tree reconciles", reconcile_file(mfile, root), None)

        mfile.write_text(json.dumps(mkdoc(m1_status="in_progress")))
        expect("state complete vs status drift", reconcile_file(mfile, root), "[state-sync]")

        mfile.write_text(json.dumps(mkdoc(m2_id="demo-slug-m3")))
        expect("id mismatch both ways", reconcile_file(mfile, root), "[id-sync]")

        # checkbox drift: doc says pending for m2; flip register m2 to in_progress
        d = mkdoc()
        d["milestones"][1]["status"] = "in_progress"
        mfile.write_text(json.dumps(d))
        expect("checkbox drift detected", reconcile_file(mfile, root), "[checkbox-sync]")

        # conditional critic file missing from critique_files
        state["critique_files"] = [
            "docs/demo-slug-m1_adversary_critique.md",
            "docs/demo-slug-m1_infra_critique.md",
        ]
        (state_dir / "state.json").write_text(json.dumps(state))
        mfile.write_text(json.dumps(mkdoc()))
        expect("missing conditional critique detected",
               reconcile_file(mfile, root), "demo-slug-m1_infra_critique.md")
        state["critique_files"] = ["docs/demo-slug-m1_adversary_critique.md"]

        # critique drift: zero out state counts
        state["critique_finding_counts"] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        (state_dir / "state.json").write_text(json.dumps(state))
        expect("critique count drift (prod-sync-m2 class)",
               reconcile_file(mfile, root), "[critique-sync]")

        state["critique_finding_counts"] = None
        (state_dir / "state.json").write_text(json.dumps(state))
        expect("null counts past critique flagged",
               reconcile_file(mfile, root), "never recorded")

        # ------------------------------------------------------ findings-sync
        state["critique_finding_counts"] = {"critical": 0, "high": 1, "medium": 2, "low": 0}
        (state_dir / "state.json").write_text(json.dumps(state))

        def mkfinding(fid: str, sev: str, status: str, cfile: str) -> dict:
            return {
                "id": fid, "severity": sev, "critic": "adversary",
                "file": "x.py", "line": 1, "title": "t", "regression_guard": None,
                "critique_file": cfile, "status": status, "resolution": None,
                "history": [],
            }

        cfile = "docs/demo-slug-m1_adversary_critique.md"
        freg = {
            "schema_version": 1, "milestone_id": "demo-slug-m1",
            "critique_files": [cfile],
            "generated_by": "self-test", "generated_at": "t", "updated_at": "t",
            "findings": [
                mkfinding("H1", "HIGH", "fixed", cfile),
                mkfinding("M1", "MEDIUM", "fixed", cfile),
                mkfinding("M2", "MEDIUM", "deferred", cfile),
            ],
        }
        fpath = state_dir / "findings.json"
        fpath.write_text(json.dumps(freg))
        state["fixed_findings"] = ["H1", "M1"]
        state["deferred_findings"] = ["M2"]
        state["invalidated_findings"] = []
        (state_dir / "state.json").write_text(json.dumps(state))
        expect("findings: clean register reconciles",
               check_findings(state_dir, root), None)

        freg["findings"][0]["status"] = "open"
        fpath.write_text(json.dumps(freg))
        expect("findings: open HIGH at complete flagged",
               check_findings(state_dir, root), "open CRITICAL/HIGH")
        freg["findings"][0]["status"] = "fixed"

        freg["findings"][1]["severity"] = "LOW"  # per-file tally now drifts
        fpath.write_text(json.dumps(freg))
        expect("findings: per-file count drift flagged",
               check_findings(state_dir, root), "declares")
        freg["findings"][1]["severity"] = "MEDIUM"

        freg["critique_files"] = [cfile, "docs/demo-slug-m1_infra_critique.md"]
        fpath.write_text(json.dumps(freg))
        expect("findings: missing critique file flagged",
               check_findings(state_dir, root), "not found")
        freg["critique_files"] = [cfile]

        state["fixed_findings"] = ["H1"]  # register says H1+M1 fixed
        (state_dir / "state.json").write_text(json.dumps(state))
        fpath.write_text(json.dumps(freg))
        expect("findings: state array vs register drift flagged",
               check_findings(state_dir, root), "fixed_findings")

        # round-4 F2 mirror: unknown status flagged advisorily
        state["fixed_findings"] = ["H1", "M1"]
        (state_dir / "state.json").write_text(json.dumps(state))
        freg["findings"][0]["status"] = "opened"
        fpath.write_text(json.dumps(freg))
        expect("findings: unknown status flagged (F2)",
               check_findings(state_dir, root), "unknown status")
        freg["findings"][0]["status"] = "fixed"
        fpath.unlink()  # leave the earlier fixtures unaffected

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    repo_root: Path | None = None
    slug: str | None = None
    i = 1
    while i < len(argv):
        if argv[i] == "--repo-root" and i + 1 < len(argv):
            repo_root = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--slug" and i + 1 < len(argv):
            slug = argv[i + 1]
            i += 2
        else:
            print(__doc__, file=sys.stderr)
            return 2
    root = repo_root if repo_root else _find_repo_root()
    if not root.is_dir():
        print(f"repo root not a directory: {root}", file=sys.stderr)
        return 2
    return run(root, slug)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
