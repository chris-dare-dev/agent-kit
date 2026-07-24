#!/usr/bin/env python3
"""Single sanctioned writer for milestone status in .claude/notes/roadmaps/<slug>/milestones.json.

Usage:
  roadmap-milestones-status.py <milestones.json> <id> <new-status>
      [--override "reason"] [--field run.<key>=<value> ...] [--check-only]
  roadmap-milestones-status.py <milestones.json> <id> --get
  roadmap-milestones-status.py --find <repo-root> <id>
  roadmap-milestones-status.py --self-test

Status state machine (schema: data/references/roadmap-milestones-schema.md):

  pending -> in_progress -> complete   (terminal)
  pending|in_progress -> cancelled
  cancelled -> pending                 (reopen; clears run.* metadata)

Rules:
  - Setting the CURRENT status again is an idempotent no-op (exit 0) — resume-safe.
  - 'complete' is terminal; nothing leaves it.
  - DEPENDENCY GATE: pending -> in_progress refuses (exit 3) while any depends_on
    milestone is not 'complete'. Pass --override "<reason>" to proceed anyway; the
    override is recorded in history[].reason and run.override — audited, never silent.
  - --check-only dry-runs the whole decision (legality + dep-gate) with the same exit
    codes and NO write — init scripts gate first, create state, then write for real.
  - Reopen (cancelled -> pending) resets run.* to nulls; history[] keeps the audit trail.
  - Timestamps auto-set: run.started_at on -> in_progress, run.completed_at on -> complete.
  - --field writes are restricted to run.* keys (string values). Everything else in the
    file belongs to the materializer-merge or the --gitlab flow — not this script.
  - --find scans <repo-root>/.claude/notes/roadmaps/*/milestones.json for the id and
    prints the file path (exit 0), or exits 1 if absent, or exits 2 if AMBIGUOUS
    (multiple registers claim the id) — lets bash callers stay JSON-free.
  - Refuses registers whose schema_version != 1 (see schema doc: Versioning).
  - Concurrency: exclusive fcntl lock on <register>.lock around read-modify-write;
    the write itself is atomic (temp + os.replace).

The register is LOCAL-ONLY, never committed (policy 2026-07-09).

Exit codes: 0 ok/no-op | 1 illegal transition or not found | 2 usage/ambiguous | 3 dep-gate refusal.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUSES = {"pending", "in_progress", "complete", "cancelled"}

# A depends_on entry matching this shape is a SPIKE prerequisite, resolved from
# the spike's local state.json rather than from a milestone row in this register
# (Tier 3). Same shape as validate-spike-id.sh / spike-checkpoint.py — keep in sync.
SPIKE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*-spike-[0-9]+$")

# from-status -> set of legal to-statuses
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "cancelled"},
    "in_progress": {"complete", "cancelled"},
    "cancelled": {"pending"},
    "complete": set(),  # terminal
}

RUN_KEYS = {"state_path", "started_at", "completed_at", "rectification_commit", "override"}


def _repo_root_from_register(path: Path) -> Path | None:
    """A register lives at <repo-root>/.claude/notes/roadmaps/<slug>/milestones.json,
    so the repo root is parents[4]. Returns None if the path is not at that
    canonical depth (a spike dep then resolves as unmet-unresolvable, never a crash)."""
    p = path.resolve()
    pr = p.parents
    if len(pr) >= 5 and pr[1].name == "roadmaps" and pr[2].name == "notes" and pr[3].name == ".claude":
        return pr[4]
    return None


def _spike_dep_status(repo_root: Path | None, spike_id: str) -> tuple[bool, str]:
    """Resolve a SPIKE prerequisite from its local state.json (Tier 3).

    MET only when the spike reached the ACCEPT terminal — which itself requires a
    canonical YES/NO/UNCERTAIN verdict (spike-checkpoint.py's 'complete' gate). A
    --skip-review / capped / aborted / not-yet-run spike is NOT met, so it cannot
    silently unblock the milestone (override-able, audited). The verdict CONTENT
    (YES vs NO) does not gate: an ACCEPTed spike has ANSWERED the question, and the
    human decides via the roadmap how the milestone proceeds on that answer.
    Read-only: the spike pipeline never writes this register (loose coupling)."""
    if repo_root is None:
        return False, "unresolvable (register not at canonical .claude/notes/roadmaps path)"
    sp = repo_root / ".claude" / "notes" / "spikes" / spike_id / "state.json"
    if not sp.is_file():
        return False, "not run (no spike state.json)"
    try:
        st = json.loads(sp.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"spike state unreadable ({exc})"
    terminal = st.get("terminal_status")
    verdict = st.get("verdict")
    return terminal == "accept", f"spike terminal={terminal or 'in-progress'}, verdict={verdict or 'none'}"


@contextmanager
def _locked(path: Path):
    """Exclusive advisory lock for the read-modify-write window."""
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"milestones register not found: {path}")
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"not valid JSON: {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("milestones"), list):
        sys.exit(f"not a milestones register (no 'milestones' array): {path}")
    if doc.get("schema_version") != 1:
        sys.exit(
            f"unsupported schema_version {doc.get('schema_version')!r} in {path} — "
            "this writer only mutates v1 (see roadmap-milestones-schema.md: Versioning)"
        )
    return doc


def _save_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)


def _milestone(doc: dict, mid: str) -> dict | None:
    for m in doc["milestones"]:
        if isinstance(m, dict) and m.get("id") == mid:
            return m
    return None


def find_file(repo_root: Path, mid: str) -> Path | None | str:
    """Scan <repo-root>/.claude/notes/roadmaps/*/milestones.json for a milestone id.

    Returns the Path, None if absent, or the string "AMBIGUOUS" if >1 register claims it.
    """
    roadmaps = repo_root / ".claude" / "notes" / "roadmaps"
    if not roadmaps.is_dir():
        return None
    matches: list[Path] = []
    for candidate in sorted(roadmaps.glob("*/milestones.json")):
        try:
            doc = json.loads(candidate.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(doc, dict) and _milestone(doc, mid) is not None:
            matches.append(candidate)
    if not matches:
        return None
    if len(matches) > 1:
        print(
            f"AMBIGUOUS: id '{mid}' claimed by {len(matches)} registers: "
            + ", ".join(str(m) for m in matches),
            file=sys.stderr,
        )
        return "AMBIGUOUS"
    return matches[0]


def transition(
    path: Path,
    mid: str,
    new_status: str,
    override_reason: str | None,
    fields: list[tuple[str, str]],
    check_only: bool = False,
) -> int:
    if new_status not in STATUSES:
        print(f"unknown status: {new_status}. Valid: {', '.join(sorted(STATUSES))}", file=sys.stderr)
        return 2

    with _locked(path):
        return _transition_locked(path, mid, new_status, override_reason, fields, check_only)


def _transition_locked(
    path: Path,
    mid: str,
    new_status: str,
    override_reason: str | None,
    fields: list[tuple[str, str]],
    check_only: bool,
) -> int:
    doc = _load(path)
    m = _milestone(doc, mid)
    if m is None:
        print(f"milestone '{mid}' not found in {path}", file=sys.stderr)
        return 1

    cur = m.get("status")

    # Idempotent no-op — resume-safe.
    if cur == new_status:
        if check_only:
            print(f"{mid}: already '{cur}' — no-op (check-only)")
            return 0
        _apply_fields(m, fields)
        if fields:
            _save_atomic(path, doc)
        print(f"{mid}: already '{cur}' — no-op")
        return 0

    if new_status not in TRANSITIONS.get(cur, set()):
        legal = sorted(TRANSITIONS.get(cur, set())) or ["<none — terminal>"]
        print(
            f"refusing illegal transition for {mid}: {cur} -> {new_status} "
            f"(legal from '{cur}': {', '.join(legal)})",
            file=sys.stderr,
        )
        return 1

    # DEPENDENCY GATE — the load-bearing check.
    if cur == "pending" and new_status == "in_progress":
        repo_root = _repo_root_from_register(path)
        unmet = []
        for dep in m.get("depends_on", []):
            if isinstance(dep, str) and SPIKE_ID_RE.match(dep):
                # Spike prerequisite (Tier 3): met only at the spike's ACCEPT terminal.
                met, desc = _spike_dep_status(repo_root, dep)
                if not met:
                    unmet.append(f"{dep} ({desc})")
            else:
                # Milestone prerequisite: met when 'complete' (unchanged).
                dep_m = _milestone(doc, dep)
                dep_status = dep_m.get("status") if dep_m else "<missing>"
                if dep_status != "complete":
                    unmet.append(f"{dep} (status: {dep_status})")
        if unmet and override_reason is None:
            print(f"DEP-GATE: refusing to start {mid} — unmet dependencies:", file=sys.stderr)
            for u in unmet:
                print(f"  - {u}", file=sys.stderr)
            print(
                "Complete the dependencies first, or override with an audited reason:\n"
                f'  roadmap-milestones-status.py {path} {mid} in_progress --override "<reason>"',
                file=sys.stderr,
            )
            return 3
        if unmet and override_reason is not None and not check_only:
            m.setdefault("run", {})["override"] = {"at": _now(), "reason": override_reason}

    if check_only:
        print(f"{mid}: {cur} -> {new_status} — WOULD PASS (check-only, no write)")
        return 0

    now = _now()
    m.setdefault("history", []).append(
        {"at": now, "from": cur, "to": new_status, "reason": override_reason}
    )
    m["status"] = new_status
    run = m.setdefault("run", {})
    if cur == "cancelled" and new_status == "pending":
        # Reopen: clear the previous attempt's metadata; history keeps the audit trail.
        for k in RUN_KEYS:
            run[k] = None
    if new_status == "in_progress" and not run.get("started_at"):
        run["started_at"] = now
    if new_status == "complete":
        run["completed_at"] = now
    _apply_fields(m, fields)

    _save_atomic(path, doc)
    print(f"{mid}: {cur} -> {new_status} @ {now}" + (" (OVERRIDE)" if override_reason else ""))
    return 0


def _apply_fields(m: dict, fields: list[tuple[str, str]]) -> None:
    run = m.setdefault("run", {})
    for key, value in fields:
        run[key] = value


def parse_fields(raw: list[str]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for expr in raw:
        if "=" not in expr:
            sys.exit(f"--field must be run.<key>=<value>, got: {expr}")
        key, value = expr.split("=", 1)
        key = key.strip()
        if not key.startswith("run."):
            sys.exit(f"--field may only write run.* keys, got: {key}")
        subkey = key[len("run."):]
        if subkey not in RUN_KEYS - {"override"}:
            sys.exit(
                f"--field: unknown run key '{subkey}'. "
                f"Valid: {', '.join(sorted(RUN_KEYS - {'override'}))}"
            )
        fields.append((subkey, value))
    return fields


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    import tempfile

    failures = 0

    def expect(name: str, got: int, want: int) -> None:
        nonlocal failures
        ok = got == want
        print(f"  {name}: {'ok' if ok else f'FAIL (exit {got}, wanted {want})'}")
        if not ok:
            failures += 1

    print("self-test: roadmap-milestones-status.py")
    with tempfile.TemporaryDirectory() as td:
        reg_dir = Path(td) / ".claude" / "notes" / "roadmaps" / "demo-slug"
        reg_dir.mkdir(parents=True)
        path = reg_dir / "milestones.json"

        def milestone(n: int, deps: list[str]) -> dict:
            return {
                "id": f"demo-slug-m{n}",
                "title": f"Thing {n}",
                "epic": f"E{n}",
                "lane": "now",
                "status": "pending",
                "depends_on": deps,
                "tags": ["moscow/must"],
                "rice": 1,
                "specialist": "general-purpose",
                "repos": ["tools/claude-mcp-server"],
                "external_writes": [],
                "gitlab": {"epic_iid": None, "story_iids": []},
                "run": {
                    "state_path": None,
                    "started_at": None,
                    "completed_at": None,
                    "rectification_commit": None,
                    "override": None,
                },
                "history": [],
            }

        doc = {
            "schema_version": 1,
            "slug": "demo-slug",
            "roadmap_doc": "plans/demo-slug-roadmap.md",
            "generated_by": "self-test",
            "generated_at": _now(),
            "milestones": [
                milestone(1, []),
                milestone(2, ["demo-slug-m1"]),
                milestone(3, ["demo-slug-spike-1"]),  # spike prerequisite (Tier 3)
                milestone(4, ["demo-slug-spike-2"]),  # spike prerequisite (absent, for override)
            ],
        }
        path.write_text(json.dumps(doc, indent=2))

        expect("check-only blocks m2 (no write)", transition(path, "demo-slug-m2", "in_progress", None, [], check_only=True), 3)
        d = json.loads(path.read_text())
        ok = _milestone(d, "demo-slug-m2")["status"] == "pending" and not _milestone(d, "demo-slug-m2")["history"]
        print(f"  check-only left file untouched: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        expect("m2 blocked by dep-gate", transition(path, "demo-slug-m2", "in_progress", None, []), 3)
        expect("m2 override passes gate", transition(path, "demo-slug-m2", "in_progress", "hotfix test", []), 0)
        d = json.loads(path.read_text())
        m2 = _milestone(d, "demo-slug-m2")
        ok = m2["run"]["override"] and m2["history"][-1]["reason"] == "hotfix test"
        print(f"  override audited in history+run: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        expect("m1 starts cleanly (no deps)", transition(path, "demo-slug-m1", "in_progress", None, []), 0)
        expect("same-status no-op", transition(path, "demo-slug-m1", "in_progress", None, []), 0)
        expect("m1 completes", transition(path, "demo-slug-m1", "complete", None, [("rectification_commit", "abc1234")]), 0)
        expect("complete is terminal", transition(path, "demo-slug-m1", "in_progress", None, []), 1)
        expect("backward refused", transition(path, "demo-slug-m2", "pending", None, []), 1)
        expect("cancel in_progress ok", transition(path, "demo-slug-m2", "cancelled", None, []), 0)
        expect("reopen cancelled ok", transition(path, "demo-slug-m2", "pending", None, []), 0)
        d = json.loads(path.read_text())
        m2 = _milestone(d, "demo-slug-m2")
        ok = all(m2["run"][k] is None for k in m2["run"]) and len(m2["history"]) >= 3
        print(f"  reopen cleared run.* (audit kept): {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1
        expect("unknown milestone", transition(path, "demo-slug-m9", "in_progress", None, []), 1)

        # --- spike-prerequisite dep-gate (Tier 3) ------------------------------
        spikes = Path(td) / ".claude" / "notes" / "spikes"

        def write_spike(sid: str, terminal: str, verdict: str) -> None:
            sd = spikes / sid
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "state.json").write_text(json.dumps(
                {"schema_version": 1, "spike_id": sid, "terminal_status": terminal, "verdict": verdict}
            ))

        expect("m3 blocked: spike not run", transition(path, "demo-slug-m3", "in_progress", None, []), 3)
        write_spike("demo-slug-spike-1", "skip-review", "YES")
        expect("m3 blocked: --skip-review is not ACCEPT", transition(path, "demo-slug-m3", "in_progress", None, []), 3)
        write_spike("demo-slug-spike-1", "rerun-cap", "NO")
        expect("m3 blocked: capped spike not ACCEPT", transition(path, "demo-slug-m3", "in_progress", None, []), 3)
        write_spike("demo-slug-spike-1", "accept", "NO")
        expect("m3 starts: spike ACCEPTed (a NO still answers the question)", transition(path, "demo-slug-m3", "in_progress", None, []), 0)
        expect("m4 blocked: spike-2 absent", transition(path, "demo-slug-m4", "in_progress", None, []), 3)
        expect("m4 override passes spike gate", transition(path, "demo-slug-m4", "in_progress", "verified spike manually", []), 0)
        d = json.loads(path.read_text())
        m4 = _milestone(d, "demo-slug-m4")
        ok = bool(m4["run"]["override"]) and m4["history"][-1]["reason"] == "verified spike manually"
        print(f"  spike-dep override audited: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        d = json.loads(path.read_text())
        m1 = _milestone(d, "demo-slug-m1")
        ok = (
            m1["run"]["started_at"]
            and m1["run"]["completed_at"]
            and m1["run"]["rectification_commit"] == "abc1234"
            and len(m1["history"]) == 2
        )
        print(f"  timestamps + fields recorded: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        found = find_file(Path(td), "demo-slug-m1")
        print(f"  --find locates register: {'ok' if found == path else 'FAIL'}")
        failures += 0 if found == path else 1
        found = find_file(Path(td), "other-m1")
        print(f"  --find misses unknown id: {'ok' if found is None else 'FAIL'}")
        failures += 0 if found is None else 1

        # Ambiguity: a second register claiming the same id.
        dup_dir = Path(td) / ".claude" / "notes" / "roadmaps" / "other-slug"
        dup_dir.mkdir(parents=True)
        (dup_dir / "milestones.json").write_text(json.dumps(doc))
        found = find_file(Path(td), "demo-slug-m1")
        print(f"  --find flags ambiguous id: {'ok' if found == 'AMBIGUOUS' else 'FAIL'}")
        failures += 0 if found == "AMBIGUOUS" else 1

        # Version guard.
        bad = dict(doc)
        bad["schema_version"] = 2
        (dup_dir / "milestones.json").write_text(json.dumps(bad))
        try:
            _load(dup_dir / "milestones.json")
            print("  v2 register refused by writer: FAIL (no exit)")
            failures += 1
        except SystemExit:
            print("  v2 register refused by writer: ok")

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--self-test":
        return self_test()

    if len(argv) >= 2 and argv[1] == "--find":
        if len(argv) != 4:
            print("usage: roadmap-milestones-status.py --find <repo-root> <id>", file=sys.stderr)
            return 2
        found = find_file(Path(argv[2]), argv[3])
        if found is None:
            return 1
        if found == "AMBIGUOUS":
            return 2
        print(found)
        return 0

    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2

    path, mid, action = Path(argv[1]), argv[2], argv[3]

    if action == "--get":
        doc = _load(path)
        m = _milestone(doc, mid)
        if m is None:
            print(f"milestone '{mid}' not found in {path}", file=sys.stderr)
            return 1
        print(json.dumps(m, indent=2))
        return 0

    override_reason: str | None = None
    raw_fields: list[str] = []
    check_only = False
    rest = argv[4:]
    i = 0
    while i < len(rest):
        if rest[i] == "--override":
            if i + 1 >= len(rest):
                print("--override requires a reason string", file=sys.stderr)
                return 2
            override_reason = rest[i + 1]
            i += 2
        elif rest[i] == "--field":
            if i + 1 >= len(rest):
                print("--field requires run.<key>=<value>", file=sys.stderr)
                return 2
            raw_fields.append(rest[i + 1])
            i += 2
        elif rest[i] == "--check-only":
            check_only = True
            i += 1
        else:
            print(f"unknown arg: {rest[i]}", file=sys.stderr)
            return 2

    return transition(path, mid, action, override_reason, parse_fields(raw_fields), check_only)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
