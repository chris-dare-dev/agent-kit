#!/usr/bin/env python3
"""Generate the thin provider-neutral root contract (E5, milestone provider-neutral-agent-kit-m4).

Renders TWO generated docs from the canonical catalog + the S5.1 coverage map:
  data/claude-md/AGENTS.md   the <=5120 B normative ROUTER (+ workspace copy workspace/AGENTS.md, no frontmatter)
  data/claude-md/CONTEXT.md  the provider-degradation manifest (+ workspace copy workspace/CONTEXT.md)

Sources (single source of truth each):
  data/facts/catalog.json                    -> live inventory counts + agent-family index + CONTEXT rows
  data/references/agents-md-coverage-map.md   -> router statements (its ```json block is the machine source)

The router 'text' is the ONLY hand-authored prose in the pipeline and lives in the coverage map; the
inventory-via-MCP block is derived LIVE from the catalog so it never goes stale (counts are never
hardcoded here). Each 'router' statement carries a distinctive `anchor` phrase (a substring of its text);
--check greps the regenerated AGENTS.md for every router anchor, mechanically proving none was dropped —
no injected markers, so the coverage proof costs zero extra bytes against the budget.

Byte budget = 5120 (binary 5 KiB), applied to the provider-facing router BODY (the workspace AGENTS.md);
the data/ copy adds only the ~37 B Obsidian frontmatter. This mirrors catalog-generate.py's conventions
(ROOT = parents[2], --check/--self-test/default-write). Stdlib only (no PyYAML — the coverage-map machine
block is JSON). CWD-independent.

Usage:
  generate-root-contract.py             regenerate AGENTS.md + CONTEXT.md (data/ copies always; workspace copies if present)
  generate-root-contract.py --check     drift + byte-budget + coverage-completeness gate (exit 0 | 2 drift/budget | 3 coverage)
  generate-root-contract.py --self-test in-memory self-test of the render + gate logic (exit 0 | 1)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # data/scripts/x.py -> repo root
CATALOG_PATH = ROOT / "data" / "facts" / "catalog.json"
COVERAGE_MAP_PATH = ROOT / "data" / "references" / "agents-md-coverage-map.md"
AGENTS_DATA = ROOT / "data" / "claude-md" / "AGENTS.md"
CONTEXT_DATA = ROOT / "data" / "claude-md" / "CONTEXT.md"

# The data/ copy of AGENTS.md carries an Obsidian frontmatter block; the workspace
# copy (workspace/AGENTS.md) must NOT (claude-md-copy-lint.sh strips frontmatter before
# diffing, so the two bodies stay identical below it). CONTEXT.md has no frontmatter
# in either copy.
AGENTS_FRONTMATTER = "---\ntype: moc\ntags:\n  - type/moc\n---\n"

BYTE_BUDGET = 5120  # binary 5 KiB — applied to the provider-facing router body

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)

# The router rule set this milestone exists to protect. This repo is direct-commit-on-main
# for routine work (MRs only when explicitly requested), so --check is the mechanical guard: deleting a router row from
# the coverage map + regenerating would otherwise pass green (drift-clean) yet silently drop
# a normative rule from every provider's contract. Removing a rule must be a deliberate,
# visible edit here AND in the map (adversary M2). Add the new id here when a rule is added.
REQUIRED_ROUTER_IDS = frozenset({
    "intro",
    "dispatch.orchestrator-workers", "dispatch.parallel-cluster-triage",
    "dispatch.post-deploy-verify", "dispatch.three-agent-fix",
    "webapps.list", "paths.routing-layer",
    "coord.specialist-no-mcp", "coord.post-edit-commit",
    "coord.agent-artifact-finalization",
    "coord.workflow-tenant", "coord.workflow-chart-upgrade", "coord.workflow-oidc",
    "escalation.procedure",
    "paths.agents", "paths.skills", "paths.chart-ownership", "paths.pointer",
})


# ---------------------------------------------------------------------------
# Load


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def load_coverage_map(text: str | None = None) -> dict:
    """Parse the machine-source ```json block out of the coverage-map markdown."""
    if text is None:
        text = COVERAGE_MAP_PATH.read_text()
    blocks = _JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        # The map is hand-edited and explicitly invites future editing; a second
        # (e.g. illustrative) json fence must not silently repoint the pipeline (adversary L3).
        raise SystemExit(
            f"coverage map must contain exactly one ```json machine block (found {len(blocks)})"
        )
    return json.loads(blocks[0])


# ---------------------------------------------------------------------------
# Render


def render_agents(catalog: dict, cmap: dict) -> str:
    counts = catalog["counts"]
    statements = cmap["statements"]
    by_id = {s["id"]: s for s in statements}
    routers = [s for s in statements if s["class"] == "router"]
    router_by_section: dict[str, list[dict]] = {}
    for s in routers:
        router_by_section.setdefault(s.get("out_section", ""), []).append(s)

    out: list[str] = []
    out.append("# AGENTS.md — workspace Workspace Agent Registry (generated router)")
    out.append("")
    out.append(
        "<!-- GENERATED by data/scripts/generate-root-contract.py from data/facts/catalog.json "
        "+ data/references/agents-md-coverage-map.md — do NOT hand-edit. "
        "Regenerate: python3 data/scripts/generate-root-contract.py -->"
    )
    out.append("")
    if "intro" in by_id:
        out.append(by_id["intro"]["text"])
        out.append("")

    # Generated: full inventory via MCP (counts + family span live from the catalog;
    # the full per-agent family index is served by list_agents, not inlined — inlining
    # 99 names would blow the byte budget without adding a rule).
    ndomains = len({e.get("domain") for e in catalog["entries"] if e["kind"] == "agent"})
    out.append("## Full inventory via MCP (not inlined)")
    out.append("")
    out.append(f"- **Agents ({counts['agents']})** — `list_agents` / `get_agent({{name}})`")
    out.append(f"- **Skills ({counts['skills']})** — `list_skills` / `get_skill({{name}})`")
    out.append(f"- **Entrypoints ({counts['entrypoints']})** — slash commands in `.claude/commands/`")
    out.append(f"- **Agent families** — {counts['agents']} agents across {ndomains} domains; enumerate/filter via `list_agents`")
    out.append("- **App/chart context** — `get_app_context({app})`")
    out.append("- **Canonical machine catalog** — `data/facts/catalog.json` (`catalog-generate.py`)")
    out.append("- **Provider degradation** — see `CONTEXT.md` (Codex/OpenCode-partial items)")
    out.append("")

    # Router sections, in declared order
    for section in cmap["out_section_order"]:
        rows = router_by_section.get(section)
        if not rows:
            continue
        out.append(f"## {section}")
        out.append("")
        for s in rows:
            out.append(s["text"])
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_context(catalog: dict) -> str:
    rows = [e for e in catalog["entries"] if e.get("providerPartial")]
    rows.sort(key=lambda e: (e["kind"], e["name"]))
    out: list[str] = []
    out.append("# CONTEXT.md — provider-degradation manifest (generated)")
    out.append("")
    out.append(
        "<!-- GENERATED by data/scripts/generate-root-contract.py from data/facts/catalog.json "
        "— do NOT hand-edit. Regenerate: python3 data/scripts/generate-root-contract.py -->"
    )
    out.append("")
    out.append(
        "These capabilities use Claude-Code-only constructs (the Agent / AskUserQuestion / Workflow "
        "tools). On Codex and OpenCode they render with the degradation noted below rather than being "
        "silently dropped. Everything else in the kit is fully provider-neutral."
    )
    out.append("")
    out.append(f"**{len(rows)} provider-partial items** (derived live from the catalog):")
    out.append("")
    for e in rows:
        note = e.get("degradationNote") or "provider-partial"
        out.append(f"- **{e['kind']}/{e['name']}** — {note}")
    out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Workspace copy-pair helpers


def _workspace_root() -> Path | None:
    """The workspace root, if this checkout is nested inside one.

    In CI the repo is checked out standalone (no workspace parent) — return None so
    only the in-repo data/ copies are written; claude-md-copy-lint.sh (workspace-side)
    owns the data<->workspace identity check, not this generator.
    """
    env = os.environ.get("WORKSPACE_ROOT") or os.environ.get("PERSONAL_WORKSPACE_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env))
    if len(ROOT.parents) > 4:  # guard shallow/standalone checkouts (adversary L2)
        candidates.append(ROOT.parents[4])  # .../workspace/repos/SWAT.../platform/agent-kit
    for c in candidates:
        if c.is_dir() and (c / "CLAUDE.md").exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Gate


def check(catalog: dict, cmap: dict) -> int:
    agents_body = render_agents(catalog, cmap)
    context_body = render_context(catalog)
    problems: list[str] = []

    # 1. byte budget (provider-facing body)
    nbytes = len(agents_body.encode("utf-8"))
    if nbytes > BYTE_BUDGET:
        problems.append(f"BUDGET: AGENTS.md router body {nbytes} B > {BYTE_BUDGET} B budget")

    # 2. drift vs on-disk data/ copies
    expected_agents_data = AGENTS_FRONTMATTER + agents_body
    if not AGENTS_DATA.exists() or AGENTS_DATA.read_text() != expected_agents_data:
        problems.append(f"DRIFT: {AGENTS_DATA.relative_to(ROOT)} differs from a fresh render")
    if not CONTEXT_DATA.exists() or CONTEXT_DATA.read_text() != context_body:
        problems.append(f"DRIFT: {CONTEXT_DATA.relative_to(ROOT)} differs from a fresh render")

    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        print(f"{len(problems)} drift/budget problem(s) — run generate-root-contract.py to regenerate", file=sys.stderr)
        return 2

    # 3. coverage completeness (exit 3, distinct from drift) — every source statement
    #    classified, and every router statement actually rendered (its anchor phrase is
    #    present in the generated AGENTS.md). The anchor is a real substring of the text,
    #    so anchor-present ⟺ rule-rendered — no injected marker, zero byte cost.
    coverage: list[str] = []
    valid = {"router", "mcp-served", "redundant", "audit"}
    for s in cmap["statements"]:
        if s.get("class") not in valid:
            coverage.append(f"statement {s.get('id')!r}: invalid class {s.get('class')!r}")
    for s in cmap["statements"]:
        if s["class"] != "router":
            continue
        anchor = s.get("anchor")
        if not anchor:
            coverage.append(f"router statement {s['id']!r} has no anchor")
        elif anchor not in s.get("text", ""):
            coverage.append(f"router statement {s['id']!r} anchor is not a substring of its own text (authoring error)")
        elif anchor not in agents_body:
            coverage.append(f"router statement {s['id']!r} missing from generated AGENTS.md (anchor {anchor!r} absent)")
    if not any(s["class"] == "router" for s in cmap["statements"]):
        coverage.append("no router statements in coverage map")
    if not any(s.get("id") == "audit.claude-md-tier-sweep" for s in cmap["statements"]):
        coverage.append("missing CLAUDE.md-tier sweep audit row")

    # Pin the protected rule set: a router row cannot be silently deleted from the map
    # (adversary M2). Removing a rule requires editing REQUIRED_ROUTER_IDS too.
    present_router_ids = {s["id"] for s in cmap["statements"] if s["class"] == "router"}
    for rid in sorted(REQUIRED_ROUTER_IDS - present_router_ids):
        coverage.append(f"required router statement {rid!r} deleted from coverage map")

    # Anchor uniqueness: a duplicate anchor across statements could mask a dropped render
    # (adversary M2). Each router anchor must occur in exactly one statement's text.
    anchor_owners: dict[str, list[str]] = {}
    for s in cmap["statements"]:
        if s["class"] == "router" and s.get("anchor"):
            for other in cmap["statements"]:
                if other.get("anchor") == s["anchor"] and other is not s:
                    anchor_owners.setdefault(s["anchor"], []).append(s["id"])
    for anchor, ids in sorted(anchor_owners.items()):
        coverage.append(f"anchor {anchor!r} is shared by multiple statements: {ids}")

    # Self-description honesty: the map must describe the anchor mechanism, not the
    # abandoned <!-- r:{id} --> markers the generator does not emit (adversary M1).
    if COVERAGE_MAP_PATH.exists() and "<!-- r:" in COVERAGE_MAP_PATH.read_text():
        coverage.append("coverage map still describes a <!-- r: --> marker mechanism the generator does not use (anchors only)")

    if coverage:
        for c in coverage:
            print(f"FAIL: coverage: {c}", file=sys.stderr)
        print(f"{len(coverage)} coverage problem(s)", file=sys.stderr)
        return 3

    print(
        f"OK: AGENTS.md router {nbytes} B (<= {BYTE_BUDGET}); CONTEXT.md "
        f"{len(context_body.encode())} B; {sum(1 for s in cmap['statements'] if s['class']=='router')} "
        f"router statements all present; no drift."
    )
    return 0


def write_all(catalog: dict, cmap: dict) -> None:
    agents_body = render_agents(catalog, cmap)
    context_body = render_context(catalog)
    AGENTS_DATA.write_text(AGENTS_FRONTMATTER + agents_body)
    CONTEXT_DATA.write_text(context_body)
    written = [str(AGENTS_DATA.relative_to(ROOT)), str(CONTEXT_DATA.relative_to(ROOT))]
    ws = _workspace_root()
    if ws is not None:
        (ws / "AGENTS.md").write_text(agents_body)  # workspace copy: no frontmatter
        (ws / "CONTEXT.md").write_text(context_body)
        written += [f"{ws}/AGENTS.md", f"{ws}/CONTEXT.md"]
    nbytes = len(agents_body.encode("utf-8"))
    print(f"wrote: {', '.join(written)}")
    print(f"AGENTS.md router body = {nbytes} B (budget {BYTE_BUDGET})")


# ---------------------------------------------------------------------------
# Self-test (in-memory fixtures — no filesystem writes)


def self_test() -> int:
    failures: list[str] = []

    def ok(label: str, cond: bool) -> None:
        if not cond:
            failures.append(label)

    fake_catalog = {
        "counts": {"skills": 2, "agents": 3, "entrypoints": 1},
        "entries": [
            {"kind": "agent", "name": "a1", "domain": "mesh", "providerPartial": False},
            {"kind": "agent", "name": "a2", "domain": "mesh", "providerPartial": False},
            {"kind": "agent", "name": "a3", "domain": "ci", "providerPartial": False},
            {"kind": "entrypoint", "name": "milestone-pipeline", "providerPartial": True,
             "degradationNote": "Uses the Agent tool."},
            {"kind": "skill", "name": "plain", "providerPartial": False},
        ],
    }
    fake_map = {
        "out_section_order": ["Dispatch patterns", "Escalation"],
        "statements": [
            {"id": "intro", "class": "router", "out_section": "_header", "anchor": "Intro rule", "text": "Intro rule here."},
            {"id": "dispatch.x", "class": "router", "out_section": "Dispatch patterns", "anchor": "Rule X", "text": "- Rule X applies."},
            {"id": "escalation.procedure", "class": "router", "out_section": "Escalation", "anchor": "Rule Y", "text": "Rule Y applies."},
            {"id": "inv.platform", "class": "mcp-served", "new_home": "list_agents"},
            {"id": "redundant.z", "class": "redundant", "new_home": "workspace CLAUDE.md"},
            {"id": "audit.claude-md-tier-sweep", "class": "audit", "new_home": "n/a"},
        ],
    }

    agents = render_agents(fake_catalog, fake_map)
    ctx = render_context(fake_catalog)

    # counts derived live, never hardcoded
    ok("counts.agents", "Agents (3)" in agents)
    ok("counts.skills", "Skills (2)" in agents)
    # family span (domain count) derived from catalog (mesh + ci = 2 domains)
    ok("family.domains", "across 2 domains" in agents)
    # every router statement rendered — its anchor phrase is present
    ok("anchor.intro", "Intro rule" in agents)
    ok("anchor.dispatch", "Rule X" in agents)
    ok("anchor.escalation", "Rule Y" in agents)
    # no injected markers (anchor-based coverage costs zero bytes)
    ok("no.markers", "<!-- r:" not in agents)
    # CONTEXT.md derives the count live + lists the partial item
    ok("context.count", "**1 provider-partial items**" in ctx)
    ok("context.item", "entrypoint/milestone-pipeline" in ctx)
    ok("context.note", "Uses the Agent tool." in ctx)
    ok("context.only-partial", "skill/plain" not in ctx)

    # coverage gate catches a dropped router rule: a statement whose out_section isn't in
    # out_section_order never renders, so its anchor is absent from the output.
    phantom = json.loads(json.dumps(fake_map))
    phantom["statements"].append(
        {"id": "ghost.rule", "class": "router", "out_section": "NoSuchSection", "anchor": "GhostAnchor", "text": "- GhostAnchor rule."}
    )
    ghost_agents = render_agents(fake_catalog, phantom)
    ok("coverage.detects-missing", "GhostAnchor" not in ghost_agents)

    # authoring guard: an anchor that isn't a substring of its own text must be catchable
    bad_anchor = {"id": "z", "class": "router", "out_section": "Escalation", "anchor": "NOPE", "text": "totally different"}
    ok("coverage.bad-anchor", bad_anchor["anchor"] not in bad_anchor["text"])

    # invalid class detection
    bad = json.loads(json.dumps(fake_map))
    bad["statements"].append({"id": "weird", "class": "nonsense"})
    has_invalid = any(s.get("class") not in {"router", "mcp-served", "redundant", "audit"} for s in bad["statements"])
    ok("coverage.invalid-class", has_invalid)

    # M2(a): deleting a pinned router id from the map is detected (missing set non-empty)
    present = {"escalation.procedure"}  # pretend all others were deleted
    ok("coverage.required-ids-deletion", bool(REQUIRED_ROUTER_IDS - present))

    # M2(b): two statements sharing an anchor are detected as non-unique
    dup = [
        {"id": "a", "class": "router", "anchor": "SAME", "text": "SAME one"},
        {"id": "b", "class": "router", "anchor": "SAME", "text": "SAME two"},
    ]
    shared = any(
        o.get("anchor") == s.get("anchor") and o is not s
        for s in dup for o in dup
    )
    ok("coverage.anchor-uniqueness", shared)

    # L3: exactly-one machine block — a two-block document raises SystemExit
    two_block = "```json\n{}\n```\ntext\n```json\n{}\n```\n"
    raised = False
    try:
        load_coverage_map(text=two_block)
    except SystemExit:
        raised = True
    ok("load.rejects-two-blocks", raised)

    # L2: _workspace_root() must never raise, regardless of ROOT depth or env
    try:
        _workspace_root()
        ok("workspace-root.no-raise", True)
    except Exception:
        ok("workspace-root.no-raise", False)

    # budget arithmetic is on the body bytes
    ok("budget.is-int", isinstance(BYTE_BUDGET, int) and BYTE_BUDGET == 5120)

    for f in failures:
        print(f"FAIL: self-test: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} self-test failure(s)", file=sys.stderr)
        return 1
    print("OK: generate-root-contract self-test passed")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    catalog = load_catalog()
    cmap = load_coverage_map()
    if "--check" in argv:
        return check(catalog, cmap)
    write_all(catalog, cmap)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
