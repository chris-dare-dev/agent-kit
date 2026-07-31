#!/usr/bin/env python3
"""Deterministically merge a structural draft into the live milestone register.

Usage:
  roadmap-milestones-merge.py <register-path> <incoming-path>
  roadmap-milestones-merge.py --self-test

The roadmap-materializer agent AUTHORS STRUCTURE ONLY (a full v1 document whose
milestones are all pristine skeletons: status=pending, empty history, all-null run,
all-null gitlab). This script is the ONLY sanctioned writer for materialization: it
merges that draft into the live register so execution state can never be lost to a
re-materialize (schema + single-writer rules: data/references/roadmap-milestones-schema.md).

Merge rules (by milestone id):
  - id in both      -> structure fields (title, epic, lane, depends_on, tags, rice,
                       specialist, repos, external_writes) come from INCOMING;
                       state fields (status, run, history, gitlab) are PRESERVED
                       from the existing register. Never downgraded.
  - id only in new  -> adopted as-is (skeleton enforced on the way in).
  - id only in old  -> status 'pending': dropped silently (never started).
                       any other status: REFUSE (exit 3) and list them — removing a
                       started/completed/cancelled milestone from the roadmap is a
                       human decision, not a merge default.

Also refuses: slug mismatch, schema_version != 1 on either side, non-skeleton
incoming milestones (a draft carrying status/history is a contract violation).

Register absent -> the (validated-shape) incoming becomes the register.

The register is LOCAL-ONLY, never committed (policy 2026-07-09). Exclusive advisory lock
on <register>.lock; atomic temp+rename write.

Exit codes: 0 merged | 2 usage/contract violation | 3 active-milestone drop refused.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

# The file-locking primitives live in the sibling workspace-tooling tree: one
# definition, shared by both trees, so data/scripts and the substrate cannot
# drift apart (M2, gates-green-t-fcntl-datascripts). The path is derived from
# __file__ rather than guessed from the CWD -- these scripts are invoked from
# runbooks, from the gate runner and (from M3) from CI, none of which promise a
# working directory.
_WORKSPACE_TOOLING = Path(__file__).resolve().parents[2] / "workspace-tooling"
if str(_WORKSPACE_TOOLING) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_TOOLING))
import platform_compat  # noqa: E402

STRUCTURE_FIELDS = [
    "title",
    "epic",
    "lane",
    "depends_on",
    "tags",
    "rice",
    "specialist",
    "repos",
    "external_writes",
]
STATE_FIELDS = ["status", "run", "history", "gitlab"]

RUN_SKELETON = {
    "state_path": None,
    "started_at": None,
    "completed_at": None,
    "rectification_commit": None,
    "override": None,
}
GITLAB_SKELETON = {"epic_iid": None, "story_iids": []}


@contextmanager
def _locked(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lf:
        platform_compat.lock_file_exclusive(lf)
        try:
            yield
        finally:
            platform_compat.unlock_file(lf)


def _read(path: Path, label: str) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"{label}: unreadable JSON at {path}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("milestones"), list):
        sys.exit(f"{label}: not a milestones document: {path}")
    if doc.get("schema_version") != 1:
        sys.exit(
            f"{label}: unsupported schema_version {doc.get('schema_version')!r} — "
            "writers only mutate v1 (roadmap-milestones-schema.md: Versioning)"
        )
    return doc


def _check_skeleton(incoming: dict) -> list[str]:
    """The materializer authors structure only — reject drafts smuggling state."""
    problems = []
    for m in incoming["milestones"]:
        mid = m.get("id", "?")
        if m.get("status") != "pending":
            problems.append(f"{mid}: incoming status must be 'pending', got {m.get('status')!r}")
        if m.get("history") != []:
            problems.append(f"{mid}: incoming history must be []")
        if m.get("run") != RUN_SKELETON:
            problems.append(f"{mid}: incoming run must be the all-null skeleton")
        if m.get("gitlab") != GITLAB_SKELETON:
            problems.append(f"{mid}: incoming gitlab must be the all-null skeleton")
    return problems


def _save_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge(register_path: Path, incoming_path: Path) -> int:
    incoming = _read(incoming_path, "incoming")
    problems = _check_skeleton(incoming)
    if problems:
        print("incoming draft violates the structure-only contract:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    with _locked(register_path):
        if not register_path.exists():
            register_path.parent.mkdir(parents=True, exist_ok=True)
            _save_atomic(register_path, incoming)
            print(f"created register: {register_path} ({len(incoming['milestones'])} milestones)")
            return 0

        existing = _read(register_path, "register")
        if existing.get("slug") != incoming.get("slug"):
            print(
                f"slug mismatch: register '{existing.get('slug')}' vs incoming "
                f"'{incoming.get('slug')}' — wrong register path?",
                file=sys.stderr,
            )
            return 2

        old_by_id = {m["id"]: m for m in existing["milestones"] if isinstance(m, dict)}
        new_ids = {m["id"] for m in incoming["milestones"] if isinstance(m, dict)}

        # Refuse dropping anything that ever left 'pending'.
        blocked = [
            f"{mid} (status: {m.get('status')})"
            for mid, m in old_by_id.items()
            if mid not in new_ids and m.get("status") != "pending"
        ]
        if blocked:
            print("REFUSING merge — incoming draft drops non-pending milestones:", file=sys.stderr)
            for b in blocked:
                print(f"  - {b}", file=sys.stderr)
            print(
                "Keep them in the roadmap doc, or resolve their status explicitly "
                "(roadmap-milestones-status.py) before re-materializing.",
                file=sys.stderr,
            )
            return 3

        merged_list = []
        preserved = adopted = 0
        for m in incoming["milestones"]:
            old = old_by_id.get(m["id"])
            if old is None:
                merged_list.append(m)
                adopted += 1
                continue
            merged = dict(m)  # structure from incoming
            for f in STATE_FIELDS:
                merged[f] = old.get(f, m.get(f))  # state preserved
            merged_list.append(merged)
            preserved += 1

        out = dict(incoming)
        out["milestones"] = merged_list
        dropped = len(old_by_id) - preserved
        _save_atomic(register_path, out)
        print(
            f"merged register: {register_path} — {preserved} state-preserved, "
            f"{adopted} new, {dropped} pending-dropped"
        )
        return 0


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

    def milestone(n: int, title: str = "", deps: list[str] | None = None) -> dict:
        return {
            "id": f"demo-slug-m{n}",
            "title": title or f"Thing {n}",
            "epic": f"E{n}",
            "lane": "now",
            "status": "pending",
            "depends_on": deps or [],
            "tags": ["moscow/must"],
            "rice": 1,
            "specialist": "general-purpose",
            "repos": ["agent-kit"],
            "external_writes": [],
            "gitlab": dict(GITLAB_SKELETON),
            "run": dict(RUN_SKELETON),
            "history": [],
        }

    def doc(*ms: dict) -> dict:
        return {
            "schema_version": 1,
            "slug": "demo-slug",
            "roadmap_doc": "plans/demo-slug-roadmap.md",
            "generated_by": "self-test",
            "generated_at": "2026-07-09T00:00:00Z",
            "milestones": list(ms),
        }

    print("self-test: roadmap-milestones-merge.py")
    with tempfile.TemporaryDirectory() as td:
        reg = Path(td) / ".claude" / "notes" / "roadmaps" / "demo-slug" / "milestones.json"
        inc = Path(td) / "incoming.json"

        inc.write_text(json.dumps(doc(milestone(1), milestone(2, deps=["demo-slug-m1"]))), encoding="utf-8")
        expect("create-on-absent", merge(reg, inc), 0)

        # Simulate execution state, then re-materialize with a title change + new m3.
        live = json.loads(reg.read_text(encoding="utf-8"))
        live["milestones"][0]["status"] = "complete"
        live["milestones"][0]["run"]["rectification_commit"] = "abc1234"
        live["milestones"][0]["history"] = [
            {"at": "t", "from": "pending", "to": "in_progress", "reason": None},
            {"at": "t", "from": "in_progress", "to": "complete", "reason": None},
        ]
        reg.write_text(json.dumps(live), encoding="utf-8")

        inc.write_text(
            json.dumps(doc(milestone(1, title="Retitled"), milestone(2, deps=["demo-slug-m1"]), milestone(3))),
            encoding="utf-8",
        )
        expect("merge preserves + adopts", merge(reg, inc), 0)
        merged = json.loads(reg.read_text(encoding="utf-8"))
        m1 = merged["milestones"][0]
        ok = (
            m1["title"] == "Retitled"
            and m1["status"] == "complete"
            and m1["run"]["rectification_commit"] == "abc1234"
            and len(m1["history"]) == 2
            and len(merged["milestones"]) == 3
        )
        print(f"  structure updated, state preserved: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        # Dropping the completed m1 must refuse; dropping pending m3 is fine.
        inc.write_text(json.dumps(doc(milestone(2, deps=[]))), encoding="utf-8")
        expect("refuses dropping non-pending", merge(reg, inc), 3)
        inc.write_text(json.dumps(doc(milestone(1), milestone(2, deps=["demo-slug-m1"]))), encoding="utf-8")
        expect("drops pending silently", merge(reg, inc), 0)
        ok = len(json.loads(reg.read_text(encoding="utf-8"))["milestones"]) == 2
        print(f"  pending m3 dropped: {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        # Contract violations.
        bad = doc(milestone(1))
        bad["milestones"][0]["status"] = "in_progress"
        inc.write_text(json.dumps(bad), encoding="utf-8")
        expect("refuses stateful draft", merge(reg, inc), 2)

        bad = doc(milestone(1))
        bad["slug"] = "other-slug"
        inc.write_text(json.dumps(bad), encoding="utf-8")
        expect("refuses slug mismatch", merge(reg, inc), 2)

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--self-test":
        return self_test()
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    register_path, incoming_path = Path(argv[1]), Path(argv[2])
    if not incoming_path.exists():
        print(f"incoming draft not found: {incoming_path}", file=sys.stderr)
        return 2
    return merge(register_path, incoming_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
