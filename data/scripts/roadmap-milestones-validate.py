#!/usr/bin/env python3
"""Validate a .claude/notes/roadmaps/<slug>/milestones.json register against schema v1.

Usage:
  roadmap-milestones-validate.py <path-to-milestones.json> [--allow CHECK[,CHECK...]]
  roadmap-milestones-validate.py --self-test

Checks (schema: data/references/roadmap-milestones-schema.md):
  top-level        Required keys present, no unknown keys, schema_version == 1
  slug             Slug format + path agreement (.../roadmaps/<slug>/milestones.json)
  milestone-shape  Per-milestone required keys, types, no unknown keys
  id-format        Every id is <slug>-m<N> and unique
  enums            lane/status values legal
  epic-format      epic matches E<N>
  deps             depends_on ids exist, no self-reference, graph is a DAG
  tags             kebab-case namespace/value; exactly one moscow/* tag
  gitlab-shape     gitlab.epic_iid int-or-null, story_iids int array
  run-shape        run.* keys present, string-or-null (override object-or-null)
  history-shape    history entries carry at/from/to/reason

Structural validity ONLY — content policy (MoSCoW must-cap, RICE ranking) stays with
roadmap-score-moscow.py / roadmap-score-rice.py. One authority per check.

The register is LOCAL-ONLY, never committed (policy 2026-07-09) — this validator runs
machine-side and in CI self-test mode only.

Exit code 0 if all checks pass; 1 if any check fails; 2 on usage/parse errors.
Use --allow to suppress specific checks (mirrors roadmap-validate.py).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TOP_LEVEL_KEYS = {
    "schema_version",
    "slug",
    "roadmap_doc",
    "generated_by",
    "generated_at",
    "milestones",
}

MILESTONE_KEYS = {
    "id",
    "title",
    "epic",
    "lane",
    "status",
    "depends_on",
    "tags",
    "rice",
    "specialist",
    "repos",
    "external_writes",
    "gitlab",
    "run",
    "history",
}

RUN_KEYS = {"state_path", "started_at", "completed_at", "rectification_commit", "override"}

LANES = {"now", "next", "later"}
STATUSES = {"pending", "in_progress", "complete", "cancelled"}
MOSCOW_TAGS = {"moscow/must", "moscow/should", "moscow/could", "moscow/wont"}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
EPIC_RE = re.compile(r"^E\d+$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
# A depends_on entry of this shape is a SPIKE prerequisite (Tier 3), resolved at
# run time from the spike's state.json by roadmap-milestones-status.py — it is NOT
# a milestone row in this register, so the deps check must not flag it as dangling.
SPIKE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*-spike-[0-9]+$")


def check_top_level(doc: dict, path: Path) -> list[str]:
    failures = []
    missing = TOP_LEVEL_KEYS - doc.keys()
    unknown = doc.keys() - TOP_LEVEL_KEYS
    for k in sorted(missing):
        failures.append(f"top-level: missing required key '{k}'")
    for k in sorted(unknown):
        failures.append(f"top-level: unknown key '{k}' (add it to the schema first)")
    if doc.get("schema_version") != 1:
        failures.append(
            f"top-level: schema_version must be 1, got {doc.get('schema_version')!r}"
        )
    if not isinstance(doc.get("milestones"), list) or not doc.get("milestones"):
        failures.append("top-level: 'milestones' must be a non-empty array")
    return failures


def check_slug(doc: dict, path: Path) -> list[str]:
    failures = []
    slug = doc.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug or "") or len(slug or "") > 40:
        failures.append(f"slug: {slug!r} is not kebab-case ≤40 chars")
        return failures
    if path.name == "-":  # stdin / self-test fixtures carry no path
        return failures
    # Canonical location: .claude/notes/roadmaps/<slug>/milestones.json
    if path.name != "milestones.json" or path.parent.name != slug:
        failures.append(
            f"slug: path '{path}' does not match canonical "
            f"'.claude/notes/roadmaps/{slug}/milestones.json' (LOCAL-ONLY tier)"
        )
    return failures


def _milestones(doc: dict) -> list[dict]:
    ms = doc.get("milestones")
    return [m for m in ms if isinstance(m, dict)] if isinstance(ms, list) else []


def check_milestone_shape(doc: dict, path: Path) -> list[str]:
    failures = []
    for i, m in enumerate(_milestones(doc)):
        label = m.get("id", f"milestones[{i}]")
        missing = MILESTONE_KEYS - m.keys()
        unknown = m.keys() - MILESTONE_KEYS
        for k in sorted(missing):
            failures.append(f"milestone-shape: {label}: missing key '{k}'")
        for k in sorted(unknown):
            failures.append(f"milestone-shape: {label}: unknown key '{k}'")
        if "title" in m and (not isinstance(m["title"], str) or not m["title"].strip()):
            failures.append(f"milestone-shape: {label}: title must be a non-empty string")
        for key in ("depends_on", "tags", "repos", "external_writes"):
            if key in m and not isinstance(m[key], list):
                failures.append(f"milestone-shape: {label}: '{key}' must be an array")
        if "repos" in m and isinstance(m["repos"], list) and not m["repos"]:
            failures.append(f"milestone-shape: {label}: 'repos' must list ≥1 repo")
        if "rice" in m and m["rice"] is not None and not isinstance(m["rice"], (int, float)):
            failures.append(f"milestone-shape: {label}: 'rice' must be a number or null")
    return failures


def check_id_format(doc: dict, path: Path) -> list[str]:
    failures = []
    slug = doc.get("slug", "")
    # `[a-z]?` admits ONE optional trailing letter so letter-suffixed sub-milestone ids
    # validate as legal: `<slug>-m1b`, `<slug>-m2c`, `<slug>-m5a` (a platform-wide convention
    # — crossplane-irsa-migration m5a/b/c, dispatcher-prove-and-distribute m1b, dna-rem
    # m1b/m2b/m2c, public-edge m6a/m7b). Numeric-only ids are unaffected (`<slug>-m1`).
    id_re = re.compile(rf"^{re.escape(slug)}-m\d+[a-z]?$") if slug else None
    seen: set[str] = set()
    for m in _milestones(doc):
        mid = m.get("id")
        if not isinstance(mid, str):
            failures.append(f"id-format: milestone with non-string id: {mid!r}")
            continue
        if id_re and not id_re.match(mid):
            failures.append(f"id-format: '{mid}' does not match '<slug>-m<N>' for slug '{slug}'")
        if mid in seen:
            failures.append(f"id-format: duplicate id '{mid}'")
        seen.add(mid)
    return failures


def check_enums(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        label = m.get("id", "?")
        if m.get("lane") not in LANES:
            failures.append(f"enums: {label}: lane {m.get('lane')!r} not in {sorted(LANES)}")
        if m.get("status") not in STATUSES:
            failures.append(
                f"enums: {label}: status {m.get('status')!r} not in {sorted(STATUSES)}"
            )
    return failures


def check_epic_format(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        epic = m.get("epic")
        if not isinstance(epic, str) or not EPIC_RE.match(epic):
            failures.append(f"epic-format: {m.get('id', '?')}: epic {epic!r} must match E<N>")
    return failures


def check_deps(doc: dict, path: Path) -> list[str]:
    failures = []
    ids = {m.get("id") for m in _milestones(doc) if isinstance(m.get("id"), str)}
    graph: dict[str, list[str]] = {}
    for m in _milestones(doc):
        mid = m.get("id", "?")
        deps = m.get("depends_on", [])
        if not isinstance(deps, list):
            continue  # shape check already flagged it
        graph[mid] = [d for d in deps if isinstance(d, str)]
        for d in deps:
            if d == mid:
                failures.append(f"deps: {mid}: depends on itself")
            elif d not in ids and not (isinstance(d, str) and SPIKE_ID_RE.match(d)):
                # A spike prerequisite (…-spike-N) is legal even though it is not a
                # milestone row here; anything else must be a declared milestone id.
                failures.append(f"deps: {mid}: depends_on '{d}' not declared in this file")
    # Cycle detection (iterative DFS, three-color).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            children = [d for d in graph.get(node, []) if d in graph]
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if color[child] == GRAY:
                    failures.append(f"deps: dependency cycle involving '{child}'")
                elif color[child] == WHITE:
                    color[child] = GRAY
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                stack.pop()
    return failures


def check_tags(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        label = m.get("id", "?")
        tags = m.get("tags")
        if not isinstance(tags, list):
            continue
        for t in tags:
            if not isinstance(t, str) or not TAG_RE.match(t):
                failures.append(
                    f"tags: {label}: '{t}' is not '<namespace>/<value>' kebab-case"
                )
        moscow = [t for t in tags if isinstance(t, str) and t.startswith("moscow/")]
        if len(moscow) != 1 or moscow[0] not in MOSCOW_TAGS:
            failures.append(
                f"tags: {label}: exactly one moscow/(must|should|could|wont) tag required, got {moscow}"
            )
    return failures


def check_gitlab_shape(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        label = m.get("id", "?")
        gl = m.get("gitlab")
        if not isinstance(gl, dict):
            failures.append(f"gitlab-shape: {label}: 'gitlab' must be an object")
            continue
        if set(gl.keys()) != {"epic_iid", "story_iids"}:
            failures.append(
                f"gitlab-shape: {label}: gitlab keys must be exactly epic_iid + story_iids"
            )
            continue
        if gl["epic_iid"] is not None and not isinstance(gl["epic_iid"], int):
            failures.append(f"gitlab-shape: {label}: epic_iid must be int or null")
        if not isinstance(gl["story_iids"], list) or any(
            not isinstance(x, int) for x in gl["story_iids"]
        ):
            failures.append(f"gitlab-shape: {label}: story_iids must be an int array")
    return failures


def check_run_shape(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        label = m.get("id", "?")
        run = m.get("run")
        if not isinstance(run, dict):
            failures.append(f"run-shape: {label}: 'run' must be an object")
            continue
        missing = RUN_KEYS - run.keys()
        unknown = run.keys() - RUN_KEYS
        for k in sorted(missing):
            failures.append(f"run-shape: {label}: run missing key '{k}'")
        for k in sorted(unknown):
            failures.append(f"run-shape: {label}: run has unknown key '{k}'")
        for k in RUN_KEYS - {"override"}:
            if k in run and run[k] is not None and not isinstance(run[k], str):
                failures.append(f"run-shape: {label}: run.{k} must be string or null")
        if "override" in run and run["override"] is not None and not isinstance(
            run["override"], dict
        ):
            failures.append(f"run-shape: {label}: run.override must be object or null")
    return failures


def check_history_shape(doc: dict, path: Path) -> list[str]:
    failures = []
    for m in _milestones(doc):
        label = m.get("id", "?")
        hist = m.get("history")
        if not isinstance(hist, list):
            failures.append(f"history-shape: {label}: 'history' must be an array")
            continue
        for i, entry in enumerate(hist):
            if not isinstance(entry, dict) or set(entry.keys()) != {
                "at",
                "from",
                "to",
                "reason",
            }:
                failures.append(
                    f"history-shape: {label}: history[{i}] must carry exactly at/from/to/reason"
                )
    return failures


CHECKS = {
    "top-level": check_top_level,
    "slug": check_slug,
    "milestone-shape": check_milestone_shape,
    "id-format": check_id_format,
    "enums": check_enums,
    "epic-format": check_epic_format,
    "deps": check_deps,
    "tags": check_tags,
    "gitlab-shape": check_gitlab_shape,
    "run-shape": check_run_shape,
    "history-shape": check_history_shape,
}


def validate(doc: dict, path: Path, allowed: set[str]) -> list[tuple[str, str]]:
    all_failures: list[tuple[str, str]] = []
    for name, fn in CHECKS.items():
        if name in allowed:
            continue
        for f in fn(doc, path):
            all_failures.append((name, f))
    return all_failures


# ---------------------------------------------------------------- self-test


def _fixture(slug: str = "demo-slug") -> dict:
    def milestone(n: int, deps: list[str] | None = None) -> dict:
        return {
            "id": f"{slug}-m{n}",
            "title": f"Do thing {n}",
            "epic": f"E{n}",
            "lane": "now",
            "status": "pending",
            "depends_on": deps or [],
            "tags": ["moscow/must"],
            "rice": 100,
            "specialist": "general-purpose",
            "repos": ["agent-kit"],
            "external_writes": ["git push origin HEAD:main (agent-kit)"],
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

    return {
        "schema_version": 1,
        "slug": slug,
        "roadmap_doc": f"plans/{slug}-roadmap.md",
        "generated_by": "roadmap-materializer",
        "generated_at": "2026-07-09T00:00:00Z",
        "milestones": [milestone(1), milestone(2, deps=[f"{slug}-m1"])],
    }


def self_test() -> int:
    import copy

    stdin_path = Path("-")
    failures = 0

    def expect(name: str, doc: dict, want_check: str | None) -> None:
        nonlocal failures
        found = validate(doc, stdin_path, set())
        names = {n for n, _ in found}
        ok = (want_check is None and not found) or (
            want_check is not None and want_check in names
        )
        status = "ok" if ok else f"FAIL (got {sorted(names)})"
        print(f"  {name}: {status}")
        if not ok:
            failures += 1

    print("self-test: roadmap-milestones-validate.py")
    expect("clean fixture passes", _fixture(), None)

    d = _fixture()
    del d["milestones"][0]["title"]
    expect("missing key detected", d, "milestone-shape")

    d = _fixture()
    d["milestones"][0]["surprise"] = True
    expect("unknown key detected", d, "milestone-shape")

    d = _fixture()
    d["milestones"][1]["depends_on"] = ["demo-slug-m9"]
    expect("dangling dep detected", d, "deps")

    d = _fixture()
    d["milestones"][1]["depends_on"] = ["demo-slug-spike-1"]
    expect("spike prerequisite dep allowed (Tier 3)", d, None)

    d = _fixture()
    d["milestones"][0]["depends_on"] = ["demo-slug-m2"]
    expect("cycle detected", d, "deps")

    d = _fixture()
    d["milestones"][0]["depends_on"] = ["demo-slug-m1"]
    expect("self-dep detected", d, "deps")

    d = _fixture()
    d["milestones"][0]["status"] = "done"
    expect("bad status enum detected", d, "enums")

    d = _fixture()
    d["milestones"][0]["tags"] = ["moscow/must", "moscow/should"]
    expect("double moscow tag detected", d, "tags")

    d = _fixture()
    d["milestones"][0]["tags"] = ["urgent"]
    expect("un-namespaced tag detected", d, "tags")

    d = _fixture()
    d["milestones"][1]["id"] = "demo-slug-m1"
    expect("duplicate id detected", d, "id-format")

    d = _fixture()
    d["schema_version"] = 2
    expect("wrong schema_version detected", d, "top-level")

    d = _fixture()
    d["milestones"][0]["gitlab"] = {"epic_iid": "42", "story_iids": []}
    expect("string iid detected", d, "gitlab-shape")

    d = _fixture()
    d["milestones"][0]["run"]["override"] = "hotfix"
    expect("non-object override detected", d, "run-shape")

    print(f"self-test: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--self-test":
        return self_test()
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    allowed: set[str] = set()
    for i, arg in enumerate(argv[2:], start=2):
        if arg.startswith("--allow"):
            if "=" in arg:
                allowed.update(arg.split("=", 1)[1].split(","))
            elif i + 1 < len(argv):
                allowed.update(argv[i + 1].split(","))

    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path}: not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict):
        print(f"FAIL: {path}: top level must be a JSON object", file=sys.stderr)
        return 2

    all_failures = validate(doc, path, allowed)

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
    print(f"  roadmap-milestones-validate.py {path} --allow <check-name>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
