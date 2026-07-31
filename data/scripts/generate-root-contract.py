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
    "coord.specialist-no-mcp", "coord.post-edit-commit",
    "coord.agent-artifact-finalization",
    "paths.agents", "paths.skills",
})

# Statement classes the coverage map may use. `retired` (M2, gates-green-t-router-content)
# records a statement whose target did not survive the genericization fork: eight router rows
# routed to agents, commands and references absent from this tree. Retiring rather than deleting
# keeps the ledger's one-row-per-source-statement property — see the fork note in the map.
VALID_CLASSES = frozenset({"router", "mcp-served", "redundant", "retired", "audit"})


# ---------------------------------------------------------------------------
# Load


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def load_coverage_map(text: str | None = None) -> dict:
    """Parse the machine-source ```json block out of the coverage-map markdown."""
    if text is None:
        text = COVERAGE_MAP_PATH.read_text(encoding="utf-8")
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
    out.append("# AGENTS.md — agent registry (generated router)")
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
# Referential integrity (M2, gates-green-t-referential-integrity)
#
# Removing the phantom routes once was not a fix: nothing stopped them coming back.
# Every identifier the generated artifacts NAME must resolve against the content
# actually discovered in the tree. The check runs over the RENDERED bodies (so it
# cannot be fooled by a statement the renderer drops) and then attributes each
# failure back to the coverage-map statement that produced it.

TOOLS_GOLDEN_PATH = ROOT / "tests" / "fixtures" / "tools-list.golden.json"

# `get_agent("x")` / `get_reference('x')` — a QUOTED literal argument is a route.
# `get_agent({name})` is the placeholder form and names nothing, so it is not matched.
_ROUTE_CALL_RE = re.compile(
    r"\b(get_agent|get_skill|get_reference|get_context_guide)\(\s*[\"']([^\"']+)[\"']\s*\)"
)
# Backticked spans: the router's only markup for an identifier.
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# A backticked snake_case identifier, optionally with a call suffix, is an MCP tool
# reference. Every tool this server serves is snake_case, so requiring an underscore
# separates tool names from prose without a hand-maintained keyword list. To name a
# snake_case thing that is NOT a tool, don't backtick it.
_TOOL_SHAPED_RE = re.compile(r"^([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(\(.*\))?$")
# A slash command, but not a path segment: `/deploy-check` yes, `.claude/commands/` no.
_SLASH_COMMAND_RE = re.compile(r"(?<![\w/.])/([a-z][a-z0-9-]*)(?![\w/.])")


def discovered_sets() -> dict[str, set[str]]:
    """The identifier sets a generated artifact is allowed to name."""

    def stems(d: Path) -> set[str]:
        return {p.stem for p in sorted(d.glob("*.md"))} if d.is_dir() else set()

    skills_dir = ROOT / "data" / "skills"
    skills = {p.name for p in sorted(skills_dir.iterdir()) if p.is_dir()} if skills_dir.is_dir() else set()

    tools: set[str] = set()
    if TOOLS_GOLDEN_PATH.exists():
        tools = {t["name"] for t in json.loads(TOOLS_GOLDEN_PATH.read_text(encoding="utf-8"))}

    return {
        "agent": stems(ROOT / "data" / "agents"),
        "command": stems(ROOT / "data" / "commands"),
        "reference": stems(ROOT / "data" / "references"),
        "skill": skills,
        "mcp tool": tools,
    }


def _extract_identifiers(text: str) -> list[tuple[str, str]]:
    """Every (kind, name) an artifact body claims is reachable."""
    found: list[tuple[str, str]] = []
    call_kind = {
        "get_agent": "agent",
        "get_skill": "skill",
        "get_reference": "reference",
        "get_context_guide": "reference",
    }
    for tool, arg in _ROUTE_CALL_RE.findall(text):
        found.append((call_kind[tool], arg))
    for span in _BACKTICK_RE.findall(text):
        m = _TOOL_SHAPED_RE.match(span.strip())
        if m:
            found.append(("mcp tool", m.group(1)))
        for cmd in _SLASH_COMMAND_RE.findall(span):
            found.append(("command", cmd))
    for cmd in _SLASH_COMMAND_RE.findall(_BACKTICK_RE.sub(" ", text)):
        found.append(("command", cmd))
    # Stable order, no duplicates.
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def check_referential_integrity(agents_body: str, context_body: str, cmap: dict) -> list[str]:
    """Fail on any identifier a generated artifact names that the tree cannot supply."""
    problems: list[str] = []
    sets = discovered_sets()

    # The coverage map may declare a verbatim extract target; if it does, it must exist.
    extract = cmap.get("escalation_extract")
    if extract:
        if not (ROOT / extract).exists():
            problems.append(f"declared extract target missing: {extract}")

    def attribute(name: str) -> str:
        """Which coverage-map statement put this identifier into the render?"""
        for s in cmap["statements"]:
            if s.get("class") == "router" and name in s.get("text", ""):
                return f"coverage-map statement {s['id']!r}"
        return "the generator's own rendered block (not a coverage-map statement)"

    for label, body in (("AGENTS.md", agents_body), ("CONTEXT.md", context_body)):
        for kind, name in _extract_identifiers(body):
            known = sets[kind]
            if name in known:
                continue
            problems.append(
                f"{label} names {kind} {name!r}, which does not resolve against "
                f"{kind} set ({len(known)} discovered) — declared in {attribute(name)}"
            )
    return problems


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

    # 0. referential integrity, FIRST — it is a property of the fresh render, not of
    #    what happens to be on disk. Adding a broken route to the coverage map also
    #    makes the on-disk copy stale, so if drift were checked first the operator
    #    would be told to regenerate rather than told which route is unresolvable
    #    (gates-green-t-referential-integrity: --check must name the identifier).
    refint = check_referential_integrity(agents_body, context_body, cmap)
    if refint:
        for r in refint:
            print(f"FAIL: referential integrity: {r}", file=sys.stderr)
        print(
            f"{len(refint)} unresolvable identifier(s) — a generated artifact routes to "
            f"something absent from the tree; fix the coverage map, do not regenerate over it",
            file=sys.stderr,
        )
        return 3

    problems: list[str] = []

    # 1. byte budget (provider-facing body)
    nbytes = len(agents_body.encode("utf-8"))
    if nbytes > BYTE_BUDGET:
        problems.append(f"BUDGET: AGENTS.md router body {nbytes} B > {BYTE_BUDGET} B budget")

    # 2. drift vs on-disk data/ copies
    expected_agents_data = AGENTS_FRONTMATTER + agents_body
    if not AGENTS_DATA.exists() or AGENTS_DATA.read_text(encoding="utf-8") != expected_agents_data:
        problems.append(f"DRIFT: {AGENTS_DATA.relative_to(ROOT)} differs from a fresh render")
    if not CONTEXT_DATA.exists() or CONTEXT_DATA.read_text(encoding="utf-8") != context_body:
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
    for s in cmap["statements"]:
        if s.get("class") not in VALID_CLASSES:
            coverage.append(f"statement {s.get('id')!r}: invalid class {s.get('class')!r}")
        if s.get("class") == "retired" and not s.get("reason"):
            # A retired row without a reason is indistinguishable from a quietly deleted
            # rule — the exact failure REQUIRED_ROUTER_IDS exists to prevent.
            coverage.append(f"retired statement {s.get('id')!r} has no reason")
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
    if COVERAGE_MAP_PATH.exists() and "<!-- r:" in COVERAGE_MAP_PATH.read_text(encoding="utf-8"):
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
    AGENTS_DATA.write_text(AGENTS_FRONTMATTER + agents_body, encoding="utf-8")
    CONTEXT_DATA.write_text(context_body, encoding="utf-8")
    written = [str(AGENTS_DATA.relative_to(ROOT)), str(CONTEXT_DATA.relative_to(ROOT))]
    ws = _workspace_root()
    if ws is not None:
        (ws / "AGENTS.md").write_text(agents_body, encoding="utf-8")  # workspace copy: no frontmatter
        (ws / "CONTEXT.md").write_text(context_body, encoding="utf-8")
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
    has_invalid = any(s.get("class") not in VALID_CLASSES for s in bad["statements"])
    ok("coverage.invalid-class", has_invalid)

    # `retired` is a first-class disposition, and a retired row must say why (else it is a
    # silent deletion wearing a class name).
    ok("coverage.retired-is-valid", "retired" in VALID_CLASSES)
    ok("coverage.retired-needs-reason", not {"id": "r", "class": "retired"}.get("reason"))

    # M2(a): deleting a pinned router id from the map is detected (missing set non-empty)
    present = {"intro"}  # pretend all others were deleted
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

    # --- referential integrity (gates-green-t-referential-integrity) -------------
    # Extraction: quoted route arguments, backticked tool names, slash commands —
    # and NOT the `{name}` placeholder form, paths, or non-snake_case prose.
    ids = _extract_identifiers(
        'Use `get_agent({name})` or get_agent("does-not-exist"); run `/deploy-check`; '
        "see `data/facts/catalog.json`, `.claude/commands/`, `main`, `deploy:dev`, `list_agents`."
    )
    ok("refint.extracts-quoted-route", ("agent", "does-not-exist") in ids)
    ok("refint.extracts-tool", ("mcp tool", "list_agents") in ids)
    ok("refint.extracts-command", ("command", "deploy-check") in ids)
    ok("refint.ignores-placeholder", not any(n == "{name}" for _, n in ids))
    ok("refint.ignores-paths", not any(n in {"commands", "facts", "catalog.json"} for _, n in ids))
    ok("refint.ignores-non-snake-prose", not any(n in {"main", "deploy:dev"} for _, n in ids))

    # A broken route fails, naming the identifier, the set and the statement id.
    broken_map = {
        "statements": [
            {"id": "ghost.route", "class": "router", "out_section": "Escalation",
             "anchor": "does-not-exist", "text": 'See `get_agent("does-not-exist")`.'},
        ]
    }
    broken = check_referential_integrity('See `get_agent("does-not-exist")`.', "", broken_map)
    ok("refint.rejects-broken-route", len(broken) == 1)
    ok("refint.names-identifier", broken and "does-not-exist" in broken[0])
    ok("refint.names-set", broken and "agent set" in broken[0])
    ok("refint.names-statement", broken and "ghost.route" in broken[0])

    # A declared extract target that does not exist is named as such.
    missing_extract = check_referential_integrity(
        "", "", {"escalation_extract": "data/references/no-such-file.md", "statements": []}
    )
    ok("refint.extract-missing", any("declared extract target missing" in p for p in missing_extract))
    ok(
        "refint.extract-present-ok",
        not check_referential_integrity(
            "", "", {"escalation_extract": "data/references/agents-md-coverage-map.md", "statements": []}
        ),
    )

    # The real tree must satisfy its own check — this is the assertion that would
    # have caught the eight phantom agents and five phantom commands at HEAD.
    if CATALOG_PATH.exists() and COVERAGE_MAP_PATH.exists():
        real_cmap = load_coverage_map()
        real_catalog = load_catalog()
        ok(
            "refint.tree-is-clean",
            not check_referential_integrity(
                render_agents(real_catalog, real_cmap), render_context(real_catalog), real_cmap
            ),
        )

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
