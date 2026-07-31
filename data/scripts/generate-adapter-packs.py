#!/usr/bin/env python3
"""generate-adapter-packs.py — render the provider adapter packs from the catalog.

ONE catalog-driven compiler, three renderers (Claude / Codex / OpenCode), driven
by `data/facts/catalog.json` (S3.1/S3.2) + `data/model-policy.json` per-provider
maps (S3.8a). Supersedes `sync-codex-skills.py`'s naive `claude`->`Codex` global
substitution (the root cause of the `.Codex`/`Codex-mcp-server` broken-ref class)
with SURGICAL, per-field, per-provider transforms.

Packs (a pack file's logical path is anchor-relative — WORKSPACE/ or PLATFORM/ —
so the golden manifest is root-independent and CI-portable):

  Claude  (S3.3)  identity — the `.claude` tree IS `data/` via symlinks, so there
                  is nothing to render; `--check` proves the 1:1 catalog<->data/
                  correspondence (the no-break proof) and NEVER touches the symlinks.
  Codex   (S3.4)  WORKSPACE/.codex/agents/<name>.toml   (every catalog agent)
                  WORKSPACE/.agents/skills/<name>/...    (every catalog skill)
                  transforms: strip Agent/AskUserQuestion (skills), CLAUDE.md->
                  AGENTS.md, `.claude/<tier>/<file>` -> get_reference/get_agent/
                  get_skill (Codex HAS the MCP server) or the canonical
                  `data/<tier>/<file>` path; lowercase `.codex` ONLY (never `.Codex`).
  OpenCode(S3.5)  PLATFORM/.opencode/{agents,skills,commands}/...  (all catalog kinds)
                  OpenCode frontmatter (description / mode:subagent / resolved
                  model: / permission); refs -> canonical `data/<tier>/<file>`
                  (platform/opencode.json declares NO mcp block, so get_* would not
                  resolve). Provider-partial entries carry their degradationNote.

CI contract (S3.7, all in data-lint, python:3.12-slim — no node):
  --check   golden-manifest drift (tests/fixtures/adapter-packs.golden.json,
            logical-path -> sha256) + link-existence over rendered content +
            Linux-case (no `.Codex`/`.OpenCode`/`.Claude` literal) + 100%-coverage
            (len-diff vs catalog, no hardcoded magic numbers). Exit 0 | 2 drift |
            3 link/case/coverage.
  --update-golden   regenerate the golden manifest (routine catalog growth).
  --apply   render the packs to disk. Writes INSIDE the clone by default
            (build/provider-packs/{codex,opencode}/, git-ignored, PR-diffable);
            `--install-to WORKSPACE` writes to the resolved workspace mirrors.
            Never deletes anything unless `--prune` is passed; `--dry-run`
            previews both writes and removals.
  --self-test   exercise the transforms on torture fixtures.

Rendering is a PURE function of (catalog, data/ sources, model-policy) -> bytes, so
--check needs no workspace mirror and is CWD-independent. Stdlib + PyYAML (the
data-lint image already installs it). Workspace root (only for
`--install-to WORKSPACE`): --workspace, else $PERSONAL_WORKSPACE_ROOT /
$WORKSPACE_ROOT — and if none resolves, the command fails rather than guessing.
NOTE: --workspace moves only the WORKSPACE/ anchor (.codex/.agents); the
PLATFORM/.opencode anchor is derived from THIS repo's on-disk location, never from
--workspace, which is why WORKSPACE install mode can still write outside the clone
and OUT mode (the default) cannot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]          # data/scripts/x.py -> repo root
PLATFORM_ROOT = ROOT.parents[1]                     # platform/agent-kit -> platform/
DATA = ROOT / "data"
SKILLS_DIR = DATA / "skills"
AGENTS_DIR = DATA / "agents"
COMMANDS_DIR = DATA / "commands"
CODEX_ENTRYPOINT_ADAPTERS = DATA / "provider-adapters" / "codex" / "entrypoints"
CATALOG_PATH = DATA / "facts" / "catalog.json"
POLICY_PATH = DATA / "model-policy.json"
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "adapter-packs.golden.json"

# Claude-Code-only tools stripped from a skill's allowed-tools for Codex/OpenCode.
STRIP_TOOLS = {"Agent", "AskUserQuestion"}

# `.claude/<tier>/<file>` — the static, repo-shipped tiers a link-existence check
# can resolve. (`.claude/notes`, `.claude/agent-memory` are RUNTIME scratch paths,
# created by the agent at run time — deliberately NOT rewritten or link-checked.)
REF_RE = re.compile(r"\.claude/(skills|scripts|references|hooks|agents|commands)/([A-Za-z0-9._][A-Za-z0-9._/-]*)")

# Tiers with an MCP tool a Codex session can call to resolve the ref by name.
MCP_TIER_TOOL = {"references": "get_reference", "agents": "get_agent", "skills": "get_skill"}

# Wrong-case literals the old naive transform produced — must NEVER appear in output.
WRONGCASE_RE = re.compile(r"\.Codex|\.OpenCode|\.Claude")


# ───────────────────────── frontmatter / body ──────────────────────────────

def split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body). Tolerant of no-frontmatter files."""
    if not text.startswith("---"):
        return {}, text
    sections = re.split(r"(?m)^---[ \t]*$", text, maxsplit=2)
    if len(sections) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(sections[1])
    except yaml.YAMLError:
        fm = None
    return (fm if isinstance(fm, dict) else {}), sections[2]


def tool_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return [t.strip() for t in str(value).split(",") if t.strip()]


# ───────────────────────── ref transforms ──────────────────────────────────

def _ref_stem_for_mcp(tier: str, rest: str) -> str:
    """Bare name for an MCP tool call: strip the extension and (for skills) the
    /SKILL.md suffix; take the first path segment for references/agents."""
    if tier == "skills":
        return rest.split("/")[0]
    return re.sub(r"\.md$", "", rest.split("/")[0])


# A rest containing any of these is a runtime PLACEHOLDER/TEMPLATE (`<name>`,
# `{PHASE4_REF}`, `phase-1..4`, a glob) — rewritten so the path PREFIX is right,
# but NOT link-verified (the concrete target only exists after substitution). This
# mirrors skill-ref-check.py's `<`-placeholder tolerance, extended to {}/*/range.
_PLACEHOLDER_RE = re.compile(r"[{}<>*]|\.\.")


def rewrite_refs(text: str, provider: str) -> tuple[str, list[Path]]:
    """Rewrite `.claude/<tier>/<file>` refs for a provider and RETURN the verifiable
    targets the rewrite produced (placeholders excluded). Codex HAS the MCP server
    (.codex/config.toml) so name-addressed tiers become an MCP tool call. Codex
    script refs remain `.claude/scripts/...`: canonical bodies usually prefix
    them with WORKSPACE_ROOT, and rewriting only the suffix to `data/scripts`
    produced the invalid `${WORKSPACE_ROOT}/data/scripts` path. OpenCode has no
    MCP block, so refs become canonical `data/<tier>/<file>` paths. NEVER emits
    a `.Codex`/`.codex` path (the old bug)."""
    targets: list[Path] = []

    def sub(m: re.Match) -> str:
        tier, rest = m.group(1), m.group(2)
        trailing = ""
        while rest and rest[-1] in ").,;:":  # give back swallowed trailing punct
            trailing = rest[-1] + trailing
            rest = rest[:-1]
        # Placeholder = a template marker in the rest, or an angle-placeholder
        # immediately after the match (`.claude/scripts/argoops-<name>.sh`).
        placeholder = bool(_PLACEHOLDER_RE.search(rest)) or text[m.end():m.end() + 1] == "<"
        if provider == "codex" and tier in MCP_TIER_TOOL:
            name = _ref_stem_for_mcp(tier, rest)
            if not placeholder:
                targets.append(DATA / "skills" / name / "SKILL.md" if tier == "skills"
                               else DATA / tier / f"{name}.md")
            return f'{MCP_TIER_TOOL[tier]}("{name}"){trailing}'
        if provider == "codex" and tier in {"scripts", "commands", "hooks"}:
            if not placeholder:
                targets.append(DATA / tier / rest)
            return f".claude/{tier}/{rest}{trailing}"
        if not placeholder:
            targets.append(DATA / tier / rest)
        return f"data/{tier}/{rest}{trailing}"

    return REF_RE.sub(sub, text), targets


def strip_skill_tools(text: str) -> str:
    """Drop Claude-only tools from a SKILL.md `allowed-tools:` frontmatter line."""
    out = []
    for ln in text.splitlines(keepends=True):
        if ln.startswith("allowed-tools:"):
            prefix, _, val = ln.partition(":")
            nl = "\n" if ln.endswith("\n") else ""
            toks = [t.strip() for t in val.split(",")]
            toks = [t for t in toks if t and t not in STRIP_TOOLS]
            ln = f"{prefix}: " + ", ".join(toks) + nl
        out.append(ln)
    return "".join(out)


def transform_skill(text: str, provider: str) -> tuple[str, list[Path]]:
    """SKILL.md -> (provider mirror text, verifiable ref targets). Strip Claude-only
    tools, CLAUDE.md->AGENTS.md, provider-specific ref rewrite. NO blind Claude->Codex
    substitution (that was the bug — it mangled `agent-kit`, `.claude/scripts`)."""
    s = strip_skill_tools(text)
    s = s.replace("CLAUDE.md", "AGENTS.md")
    return rewrite_refs(s, provider)


def transform_body(body: str, provider: str) -> tuple[str, list[Path]]:
    s = body.replace("CLAUDE.md", "AGENTS.md")
    return rewrite_refs(s, provider)


def degradation_prefix(entry: dict, provider: str) -> str:
    support = entry.get("providerSupport", {}).get(provider)
    degraded = support == "degraded" if support is not None else entry.get("providerPartial")
    if degraded and entry.get("degradationNote"):
        return f"> **PROVIDER-PARTIAL:** {entry['degradationNote']}\n\n"
    return ""


# ───────────────────────── TOML rendering ──────────────────────────────────

def toml_basic_string(s: str) -> str:
    """A TOML basic (double-quoted) string, escaping the required chars."""
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_multiline(body: str) -> str:
    """A TOML multi-line string holding an agent body. Prefer a LITERAL ('''...''')
    string — it needs zero escaping, ideal for bodies with backslashes/regex — and
    fall back to an escaped basic (\"\"\"...\"\"\") string only if the body itself
    contains `'''` (which a literal string cannot represent)."""
    if "'''" not in body:
        # A leading newline right after ''' is trimmed by the TOML spec — add one so
        # the body starts on its own line (matches the existing hand-made .toml shape).
        return "'''\n" + body + ("" if body.endswith("\n") else "\n") + "'''"
    esc = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + esc + ("" if esc.endswith("\n") else "\n") + '"""'


# ───────────────────────── model-policy resolution ─────────────────────────

def load_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def codex_effort(policy: dict, model_class: str | None) -> str | None:
    if not model_class:
        return None
    return (policy["classes"].get(model_class, {}).get("providers", {})
            .get("codex", {}).get("effort"))


def opencode_model(policy: dict, model_class: str | None) -> str | None:
    if not model_class:
        return None
    return (policy["classes"].get(model_class, {}).get("providers", {})
            .get("opencode", {}).get("model"))


# ───────────────────────── source lookup ───────────────────────────────────

def _agent_source(name: str) -> Path:
    return AGENTS_DIR / f"{name}.md"


def _skill_dir(name: str) -> Path:
    return SKILLS_DIR / name


def _command_source(name: str) -> Path:
    return COMMANDS_DIR / f"{name}.md"


def _skill_assets(name: str) -> list[tuple[str, bytes]]:
    """(relpath-under-skill, bytes) for every asset file (copied verbatim)."""
    out = []
    adir = _skill_dir(name) / "assets"
    if adir.is_dir():
        for af in sorted(p for p in adir.rglob("*") if p.is_file()):
            out.append((str(af.relative_to(_skill_dir(name))), af.read_bytes()))
    return out


# ───────────────────────── renderers ───────────────────────────────────────

def render_codex(catalog: dict, policy: dict, targets: dict[str, list[Path]]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for e in catalog["entries"]:
        if e["kind"] == "agent":
            src = _agent_source(e["name"])
            _, body = split_frontmatter(src.read_text(encoding="utf-8"))
            body_t, tgts = transform_body(body.lstrip("\n"), "codex")
            body = degradation_prefix(e, "codex") + body_t
            if e.get("providerSupport", {}).get("codex") == "adapted":
                body = (
                    "> **CODEX PROMPT-POLICY ADAPTER:** Claude `tools:` restrictions are not "
                    "a Codex runtime sandbox in this pack. Obey the role's scope bounds; the "
                    "orchestrator must verify the target git status/ref before and after the task.\n\n"
                    + body
                )
            desc, desc_tgts = rewrite_refs(e["description"], "codex")
            lines = [
                f"name = {toml_basic_string(e['name'])}",
                f"description = {toml_basic_string(desc)}",
            ]
            eff = codex_effort(policy, e.get("modelClass"))
            if eff:
                lines.append(f"model_reasoning_effort = {toml_basic_string(eff)}")
            lines.append(f"developer_instructions = {toml_multiline(body)}")
            path = f"WORKSPACE/.codex/agents/{e['name']}.toml"
            files[path] = ("\n".join(lines) + "\n").encode("utf-8")
            targets[path] = tgts + desc_tgts
        elif e["kind"] == "skill":
            src = _skill_dir(e["name"]) / "SKILL.md"
            text, tgts = transform_skill(src.read_text(encoding="utf-8"), "codex")
            path = f"WORKSPACE/.agents/skills/{e['name']}/SKILL.md"
            files[path] = _prefix_body_note(text, e, "codex").encode("utf-8")
            targets[path] = tgts
            for rel, data in _skill_assets(e["name"]):
                files[f"WORKSPACE/.agents/skills/{e['name']}/{rel}"] = data
        elif e["kind"] == "entrypoint" and e.get("providerSupport", {}).get("codex") == "adapted":
            src = CODEX_ENTRYPOINT_ADAPTERS / e["name"] / "SKILL.md"
            if not src.is_file():
                raise FileNotFoundError(
                    f"catalog marks {e['id']} Codex-adapted but source is missing: {src}"
                )
            text, tgts = transform_skill(src.read_text(encoding="utf-8"), "codex")
            path = f"WORKSPACE/.agents/skills/{e['name']}/SKILL.md"
            files[path] = text.encode("utf-8")
            targets[path] = tgts
    return files


def render_opencode(catalog: dict, policy: dict, targets: dict[str, list[Path]]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for e in catalog["entries"]:
        if e["kind"] == "agent":
            src = _agent_source(e["name"])
            fm, body = split_frontmatter(src.read_text(encoding="utf-8"))
            perm = opencode_permission(fm.get("tools"))
            model = opencode_model(policy, e.get("modelClass"))
            desc, desc_tgts = rewrite_refs(e["description"], "opencode")
            fm_lines = ["---", f"description: {_yaml_scalar(desc)}", "mode: subagent"]
            if model:
                fm_lines.append(f"model: {model}")
            fm_lines.append("permission:")
            for k, v in perm.items():
                fm_lines.append(f"  {k}: {v}")
            fm_lines.append("---")
            body_t, tgts = transform_body(body.lstrip("\n"), "opencode")
            path = f"PLATFORM/.opencode/agents/{e['name']}.md"
            files[path] = ("\n".join(fm_lines) + "\n" + degradation_prefix(e, "opencode") + body_t).encode("utf-8")
            targets[path] = tgts + desc_tgts
        elif e["kind"] == "skill":
            src = _skill_dir(e["name"]) / "SKILL.md"
            text, tgts = transform_skill(src.read_text(encoding="utf-8"), "opencode")
            path = f"PLATFORM/.opencode/skills/{e['name']}/SKILL.md"
            files[path] = _prefix_body_note(text, e, "opencode").encode("utf-8")
            targets[path] = tgts
            for rel, data in _skill_assets(e["name"]):
                files[f"PLATFORM/.opencode/skills/{e['name']}/{rel}"] = data
        elif e["kind"] == "entrypoint":
            src = _command_source(e["name"])
            fm, body = split_frontmatter(src.read_text(encoding="utf-8"))
            desc, desc_tgts = rewrite_refs(e["description"], "opencode")
            fm_lines = ["---", f"description: {_yaml_scalar(desc)}", "---"]
            # Entrypoints are Gen-1/Gen-2 orchestrators — always provider-partial.
            note = degradation_prefix(e, "opencode") or (
                "> **PROVIDER-PARTIAL:** this entrypoint orchestrates via a Claude-Code-only "
                "construct (the background Workflow tool or subagent dispatch); on OpenCode it "
                "runs degraded.\n\n"
            )
            body_t, tgts = transform_body(body.lstrip("\n"), "opencode")
            path = f"PLATFORM/.opencode/commands/{e['name']}.md"
            files[path] = ("\n".join(fm_lines) + "\n" + note + body_t).encode("utf-8")
            targets[path] = tgts + desc_tgts
    return files


def _prefix_body_note(skill_text: str, entry: dict, provider: str) -> str:
    """Insert the provider-partial note at the top of a SKILL.md body (after the
    frontmatter close), leaving the frontmatter intact."""
    prefix = degradation_prefix(entry, provider)
    if not prefix:
        return skill_text
    parts = re.split(r"(?m)^---[ \t]*$", skill_text, maxsplit=2)
    if len(parts) < 3:
        return prefix + skill_text
    body = parts[2].lstrip("\n")
    return f"---{parts[1]}---\n{prefix}{body}"


def _yaml_scalar(s: str) -> str:
    """A single-line YAML double-quoted scalar (descriptions contain `: ` and quotes)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def opencode_permission(tools_raw) -> dict[str, str]:
    """Map an agent's `tools:` frontmatter to OpenCode's permission categories.
    Absent `tools:` = Claude "inherit ALL" semantics -> allow all (never a silent
    all-deny inversion); present-but-restricted -> least-privilege on the
    mutating/egress categories (read/grep/glob are non-mutating, not gated)."""
    inherit_all = tools_raw is None
    tools = set(tool_list(tools_raw))
    return {
        "edit": "allow" if (inherit_all or tools & {"Write", "Edit", "NotebookEdit"}) else "deny",
        "bash": "allow" if (inherit_all or "Bash" in tools) else "deny",
        "webfetch": "allow" if (inherit_all or tools & {"WebFetch", "WebSearch"}) else "deny",
    }


# ───────────────────────── pack assembly + checks ──────────────────────────

def build_packs(catalog: dict, policy: dict) -> tuple[dict[str, dict[str, bytes]], dict[str, list[Path]]]:
    targets: dict[str, list[Path]] = {}
    packs = {
        "codex": render_codex(catalog, policy, targets),
        "opencode": render_opencode(catalog, policy, targets),
    }
    return packs, targets


def _flatten(packs: dict[str, dict[str, bytes]]) -> dict[str, bytes]:
    flat: dict[str, bytes] = {}
    for files in packs.values():
        flat.update(files)
    return flat


def _manifest(packs: dict[str, dict[str, bytes]]) -> dict:
    flat = _flatten(packs)
    return {path: hashlib.sha256(flat[path]).hexdigest() for path in sorted(flat)}


def claude_correspondence() -> list[str]:
    """S3.3 no-break proof: the Claude pack is identity, so verify the catalog and
    the `data/` tree are in 1:1 correspondence (every catalog entry <-> a real
    source file, and no non-excluded source file is uncatalogued)."""
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    cataloged = {(e["kind"], e["name"]) for e in catalog["entries"]}
    # every catalog entry resolves to a real data/ source
    for e in catalog["entries"]:
        src = {"skill": _skill_dir(e["name"]) / "SKILL.md",
               "agent": _agent_source(e["name"]),
               "entrypoint": _command_source(e["name"])}[e["kind"]]
        if not src.is_file():
            problems.append(f"catalog entry {e['id']} has no source at {src.relative_to(ROOT)}")
        if e["kind"] == "entrypoint" and e.get("providerSupport", {}).get("codex") == "adapted":
            adapter = CODEX_ENTRYPOINT_ADAPTERS / e["name"] / "SKILL.md"
            if not adapter.is_file():
                problems.append(
                    f"catalog entry {e['id']} declares a Codex adapter but {adapter.relative_to(ROOT)} is missing"
                )
    # every non-excluded source file has a catalog entry
    def _excluded(p: Path) -> bool:
        fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        return fm.get("catalog-exclude") is True
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        fm, _ = split_frontmatter(sk.read_text(encoding="utf-8"))
        name = str(fm.get("name") or sk.parent.name)
        if not _excluded(sk) and ("skill", name) not in cataloged:
            problems.append(f"data/skills/{sk.parent.name} is uncatalogued")
    for ag in sorted(AGENTS_DIR.glob("*.md")):
        fm, _ = split_frontmatter(ag.read_text(encoding="utf-8"))
        name = str(fm.get("name") or ag.stem)
        if not _excluded(ag) and ("agent", name) not in cataloged:
            problems.append(f"data/agents/{ag.name} is uncatalogued")
    for cm in sorted(COMMANDS_DIR.glob("*.md")):
        if not _excluded(cm) and ("entrypoint", cm.stem) not in cataloged:
            problems.append(f"data/commands/{cm.name} is uncatalogued")
    return problems


def coverage_problems(catalog: dict, packs: dict[str, dict[str, bytes]]) -> list[str]:
    """100%-coverage gate — counts derived from the catalog, never hardcoded."""
    n_skill = sum(1 for e in catalog["entries"] if e["kind"] == "skill")
    n_agent = sum(1 for e in catalog["entries"] if e["kind"] == "agent")
    n_entry = sum(1 for e in catalog["entries"] if e["kind"] == "entrypoint")
    n_codex_entry = sum(
        1 for e in catalog["entries"]
        if e["kind"] == "entrypoint" and e.get("providerSupport", {}).get("codex") == "adapted"
    )
    problems: list[str] = []

    def count(pack: str, pattern: str) -> int:
        return sum(1 for p in packs[pack] if re.search(pattern, p))

    checks = [
        ("codex agents", count("codex", r"/\.codex/agents/[^/]+\.toml$"), n_agent),
        ("codex skills+adapted entrypoints", count("codex", r"/\.agents/skills/[^/]+/SKILL\.md$"), n_skill + n_codex_entry),
        ("opencode agents", count("opencode", r"/\.opencode/agents/[^/]+\.md$"), n_agent),
        ("opencode skills", count("opencode", r"/\.opencode/skills/[^/]+/SKILL\.md$"), n_skill),
        ("opencode commands", count("opencode", r"/\.opencode/commands/[^/]+\.md$"), n_entry),
    ]
    for label, got, want in checks:
        if got != want:
            problems.append(f"coverage: {label} rendered {got}, catalog has {want}")
    return problems


def linkcase_problems(packs: dict[str, dict[str, bytes]], targets: dict[str, list[Path]]) -> list[str]:
    """Linux-case + link-existence over the generated packs (S3.6 regression gate
    extended to the packs). Case: no `.Codex`/`.OpenCode`/`.Claude` literal in any
    path OR rendered content. Links: every ref the rewrite PRODUCED resolves — only
    the tracked, non-placeholder targets, so a runtime template (`{PHASE4_REF}`,
    `argoops-<name>`, an illustrative `data/commands/X.md does not exist` in prose)
    is never a false positive."""
    problems: list[str] = []
    for path, data in _flatten(packs).items():
        text = data.decode("utf-8", errors="replace")
        if WRONGCASE_RE.search(path) or WRONGCASE_RE.search(text):
            problems.append(f"{path}: wrong-case literal (.Codex/.OpenCode/.Claude) present")
    for path, tgts in targets.items():
        for t in tgts:
            if not t.exists():
                problems.append(f"{path}: rewritten ref -> missing {t.relative_to(ROOT)}")
    return problems


# ───────────────────────── modes ───────────────────────────────────────────

def _load() -> tuple[dict, dict]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8")), load_policy()


#: Default write target. Inside the clone, git-ignored, reviewable in a PR diff.
#: Before M2 the anchors resolved to `ROOT.parents[1]` and `$HOME/Work/workspace`,
#: so `--apply` on a bare clone wrote into two directories ABOVE the repository and
#: unlinked .toml files it had never created. An adopting team could not get the
#: packs into their own tree, diff them, or run the generator in CI
#: (gates-green-t-packs-out-dir).
DEFAULT_OUT = ROOT / "build" / "provider-packs"

ANCHORS = ("WORKSPACE/", "PLATFORM/")


def _resolve_workspace(arg: str | None) -> Path:
    """The workspace root for `--install-to WORKSPACE`. No silent fallback.

    `$HOME/Work/workspace` used to be the last candidate, which meant an
    unresolved workspace was not an error — it was a write into whatever
    happened to be at that path on the operator's machine.
    """
    for cand in (arg, os.environ.get("PERSONAL_WORKSPACE_ROOT"), os.environ.get("WORKSPACE_ROOT")):
        if cand and Path(cand).is_dir():
            return Path(cand)
    sys.exit(
        "could not resolve a workspace root for --install-to WORKSPACE.\n"
        "  Pass --workspace PATH, or set PERSONAL_WORKSPACE_ROOT (or WORKSPACE_ROOT).\n"
        "  Omit --install-to to write the packs inside the clone instead "
        f"(default: {DEFAULT_OUT.relative_to(ROOT).as_posix()}/)."
    )


def _strip_anchor_raises(path: str) -> bool:
    """True when `path` carries no recognised anchor (self-test helper)."""
    try:
        _strip_anchor(path)
    except ValueError:
        return True
    return False


def _strip_anchor(path: str) -> str:
    for a in ANCHORS:
        if path.startswith(a):
            return path[len(a):]
    raise ValueError(f"unanchored pack path: {path}")


def _anchor(path: str, workspace: Path) -> Path:
    if path.startswith("WORKSPACE/"):
        return workspace / path[len("WORKSPACE/"):]
    if path.startswith("PLATFORM/"):
        return PLATFORM_ROOT / path[len("PLATFORM/"):]
    raise ValueError(f"unanchored pack path: {path}")


def _destinations(
    packs: dict[str, dict[str, bytes]], install_to: str, workspace: Path | None, out: Path
) -> dict[Path, bytes]:
    """Logical pack paths -> absolute destinations, for the chosen install mode."""
    dests: dict[Path, bytes] = {}
    for provider, files in packs.items():
        for path, data in files.items():
            if install_to == "WORKSPACE":
                assert workspace is not None
                dests[_anchor(path, workspace)] = data
            else:
                # Anchor-relative under a per-provider directory, so the tree is
                # exactly what --install-to WORKSPACE would write, minus the root.
                dests[out / provider / _strip_anchor(path)] = data
    return dests


def apply(
    workspace_arg: str | None,
    install_to: str,
    out_arg: str | None,
    prune: bool,
    dry_run: bool,
) -> int:
    catalog, policy = _load()
    packs, _ = build_packs(catalog, policy)
    flat = _flatten(packs)

    workspace = _resolve_workspace(workspace_arg) if install_to == "WORKSPACE" else None
    out = Path(out_arg).resolve() if out_arg else DEFAULT_OUT
    dests = _destinations(packs, install_to, workspace, out)

    # Stale generator-owned agent .toml files. This used to run unconditionally
    # against whatever directory the workspace resolved to. Deletion is now opt-in
    # (--prune) and previewable (--prune --dry-run); without --prune nothing is
    # unlinked, ever.
    if install_to == "WORKSPACE":
        codex_agents_dir = workspace / ".codex" / "agents"  # type: ignore[union-attr]
    else:
        codex_agents_dir = out / "codex" / ".codex" / "agents"
    removed: list[Path] = []
    if prune and codex_agents_dir.is_dir():
        want = set(dests)
        for stale in sorted(codex_agents_dir.glob("*.toml")):
            if stale not in want:
                removed.append(stale)
                if not dry_run:
                    stale.unlink()

    if not dry_run:
        for dst, data in dests.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)

    n_codex = sum(1 for p in flat if p.startswith("WORKSPACE/.codex/agents/"))
    n_cxskill = sum(1 for p in flat if p.startswith("WORKSPACE/.agents/skills/") and p.endswith("SKILL.md"))
    n_oc = sum(1 for p in flat if p.startswith("PLATFORM/.opencode/agents/"))
    verb = "would write" if dry_run else "wrote"
    print(f"adapter-packs: {verb} {len(dests)} files (--install-to {install_to})")
    if install_to == "WORKSPACE":
        print(f"  codex:    {codex_agents_dir}  ({n_codex} agents, {n_cxskill} skills)")
        print(f"  opencode: {PLATFORM_ROOT / '.opencode'}  ({n_oc} agents + skills + commands)")
    else:
        print(f"  codex:    {out / 'codex'}  ({n_codex} agents, {n_cxskill} skills)")
        print(f"  opencode: {out / 'opencode'}  ({n_oc} agents + skills + commands)")
    for stale in removed:
        print(f"  {'would remove' if dry_run else 'removed'} stale: {stale}")
    if not prune and codex_agents_dir.is_dir():
        orphans = [p for p in sorted(codex_agents_dir.glob("*.toml")) if p not in dests]
        if orphans:
            print(f"  {len(orphans)} unmanaged .toml left in place — pass --prune to remove them")
    return 0


def check() -> int:
    catalog, policy = _load()
    packs, targets = build_packs(catalog, policy)

    problems: list[str] = []
    problems += [f"claude no-break: {p}" for p in claude_correspondence()]
    problems += coverage_problems(catalog, packs)
    problems += linkcase_problems(packs, targets)
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        print(f"{len(problems)} pack-contract failure(s)", file=sys.stderr)
        return 3

    # Golden-manifest drift.
    fresh = _manifest(packs)
    try:
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["files"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"FAIL: cannot read golden {GOLDEN_PATH}: {exc} — run "
              "`python3 data/scripts/generate-adapter-packs.py --update-golden`", file=sys.stderr)
        return 2
    if fresh != golden:
        added = sorted(set(fresh) - set(golden))
        removed = sorted(set(golden) - set(fresh))
        changed = sorted(p for p in set(fresh) & set(golden) if fresh[p] != golden[p])
        for p in added[:20]:
            print(f"DRIFT +new  {p}", file=sys.stderr)
        for p in removed[:20]:
            print(f"DRIFT -gone {p}", file=sys.stderr)
        for p in changed[:20]:
            print(f"DRIFT ~chg  {p}", file=sys.stderr)
        print("adapter-pack golden drift — if intentional, regenerate with "
              "`python3 data/scripts/generate-adapter-packs.py --update-golden`", file=sys.stderr)
        return 2

    counts = {k: len(v) for k, v in packs.items()}
    print(f"adapter-packs: clean — {sum(counts.values())} files "
          f"(codex {counts['codex']}, opencode {counts['opencode']}), "
          "golden + link + case + coverage all green")
    return 0


def update_golden() -> int:
    catalog, policy = _load()
    packs, _ = build_packs(catalog, policy)
    manifest = {
        "_comment": ("GENERATED by data/scripts/generate-adapter-packs.py --update-golden. "
                     "logical-path -> sha256 of each rendered pack file. Drift-gated in CI "
                     "(data-lint) via --check. Regenerate on any intentional catalog/source change."),
        "files": _manifest(packs),
    }
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN_PATH.relative_to(ROOT)} — {len(manifest['files'])} pack files hashed")
    return 0


def self_test() -> int:
    failures: list[str] = []

    def ck(label, cond):
        if not cond:
            failures.append(label)

    # ref rewrites — Codex to MCP-tool form, OpenCode to data/ path, never wrong-case
    cx, cx_t = rewrite_refs("see `.claude/references/git-topology.md` and .claude/scripts/x.py", "codex")
    ck("codex reference->get_reference", 'get_reference("git-topology")' in cx)
    ck("codex script keeps workspace symlink", ".claude/scripts/x.py" in cx)
    ck("codex no .Codex/.codex", ".Codex" not in cx and ".codex/" not in cx)
    ck("codex tracks git-topology target", (DATA / "references" / "git-topology.md") in cx_t)
    oc, _ = rewrite_refs(".claude/references/git-topology.md + .claude/agents/gitops.md", "opencode")
    ck("opencode reference->data", "data/references/git-topology.md" in oc)
    ck("opencode agent->data", "data/agents/gitops.md" in oc)
    ck("opencode no get_reference", "get_reference" not in oc)

    # placeholders/templates are rewritten (prefix fixed) but NOT link-tracked
    ph, ph_t = rewrite_refs(".claude/scripts/argoops-<name>.sh and .claude/references/x-phase-1..4.md", "codex")
    ck("placeholder script keeps workspace symlink", ".claude/scripts/argoops-" in ph)
    ck("placeholder not tracked", ph_t == [])

    # skill tool strip + CLAUDE.md rewrite, no blind claude->Codex
    sk, _ = transform_skill(
        "---\nname: d\nallowed-tools: Read, Agent, AskUserQuestion, Bash\n---\n"
        "Read the root CLAUDE.md. Uses agent-kit. See .claude/scripts/environment-facts-lint.py\n", "codex")
    ck("strips Agent+AskUserQuestion", "allowed-tools: Read, Bash\n" in sk)
    ck("CLAUDE.md->AGENTS.md", "root AGENTS.md" in sk and "CLAUDE.md" not in sk)
    ck("agent-kit preserved (NOT mangled)", "agent-kit" in sk)
    ck("script ref keeps workspace symlink", ".claude/scripts/environment-facts-lint.py" in sk)

    # TOML escaping
    ck("toml basic escapes quote+backslash",
       toml_basic_string('a "b" \\c') == '"a \\"b\\" \\\\c"')
    ck("toml literal multiline for plain body",
       toml_multiline("# T\n\nx\n").startswith("'''\n# T"))
    ck("toml basic-fallback when body has triple-quote",
       toml_multiline("has ''' in it").startswith('"""'))

    # degradation note
    e = {
        "providerPartial": True,
        "providerSupport": {"codex": "degraded", "opencode": "degraded"},
        "degradationNote": "Uses Agent.",
    }
    ck("degradation prefix", degradation_prefix(e, "codex").startswith("> **PROVIDER-PARTIAL:**"))
    ck("adapted is not degraded", degradation_prefix({**e, "providerSupport": {"codex": "adapted"}}, "codex") == "")
    ck("no degradation when not partial", degradation_prefix({"providerPartial": False}, "codex") == "")

    # OpenCode permission: absent tools = inherit-all (never silent all-deny)
    ck("no-tools -> inherit all", opencode_permission(None) == {"edit": "allow", "bash": "allow", "webfetch": "allow"})
    ck("read-only -> deny mutating/egress", opencode_permission("Read, Grep, Glob") == {"edit": "deny", "bash": "deny", "webfetch": "deny"})
    ck("bash+write+websearch -> allow", opencode_permission("Read, Bash, Write, WebSearch") == {"edit": "allow", "bash": "allow", "webfetch": "allow"})

    # --- write-target containment (gates-green-t-packs-out-dir) ----------------
    fake_packs = {
        "codex": {"WORKSPACE/.codex/agents/a.toml": b"x", "WORKSPACE/.agents/skills/s/SKILL.md": b"y"},
        "opencode": {"PLATFORM/.opencode/agents/a.md": b"z"},
    }
    out_dests = _destinations(fake_packs, "OUT", None, DEFAULT_OUT)
    ck("out: default is inside the clone", DEFAULT_OUT.is_relative_to(ROOT))
    ck("out: every destination stays under --out", all(d.is_relative_to(DEFAULT_OUT) for d in out_dests))
    ck("out: nothing escapes the repository", all(d.is_relative_to(ROOT) for d in out_dests))
    ck("out: per-provider split", DEFAULT_OUT / "codex" / ".codex" / "agents" / "a.toml" in out_dests)
    ck("out: opencode anchored under its own provider dir",
       DEFAULT_OUT / "opencode" / ".opencode" / "agents" / "a.md" in out_dests)
    ck("out: anchor prefix stripped, content preserved",
       out_dests[DEFAULT_OUT / "codex" / ".codex" / "agents" / "a.toml"] == b"x")

    # WORKSPACE mode still targets the mirrors — that is its whole purpose — but it
    # is now opt-in rather than the default.
    ws_dests = _destinations(fake_packs, "WORKSPACE", Path("/tmp/ws"), DEFAULT_OUT)
    ck("workspace: WORKSPACE/ anchors at the workspace root",
       Path("/tmp/ws/.codex/agents/a.toml") in ws_dests)
    ck("workspace: PLATFORM/ anchors at PLATFORM_ROOT",
       PLATFORM_ROOT / ".opencode" / "agents" / "a.md" in ws_dests)

    # The home-directory fallback is gone: with nothing to resolve, WORKSPACE mode
    # must fail loudly rather than write into whatever happens to be at that path.
    saved = {k: os.environ.pop(k, None) for k in ("PERSONAL_WORKSPACE_ROOT", "WORKSPACE_ROOT")}
    try:
        _resolve_workspace(None)
        ck("unresolvable workspace exits", False)
    except SystemExit as exc:
        msg = str(exc)
        ck("unresolvable workspace exits", True)
        ck("exit names --workspace", "--workspace" in msg)
        ck("exit names the env var", "PERSONAL_WORKSPACE_ROOT" in msg)
        ck("exit names the in-clone default", "provider-packs" in msg)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    ck("unanchored pack path is rejected", _strip_anchor_raises("no-anchor/x.md"))

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        print(f"{len(failures)} self-test failure(s)", file=sys.stderr)
        return 1
    print("generate-adapter-packs self-test: OK")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update-golden", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace", metavar="PATH",
                    help="workspace root for --install-to WORKSPACE (else $PERSONAL_WORKSPACE_ROOT / $WORKSPACE_ROOT)")
    ap.add_argument("--install-to", choices=("OUT", "WORKSPACE"), default="OUT",
                    help="OUT (default) writes inside the clone; WORKSPACE writes to the resolved mirrors")
    ap.add_argument("--out", metavar="DIR",
                    help=f"output directory for --install-to OUT (default {DEFAULT_OUT.relative_to(ROOT).as_posix()}/)")
    ap.add_argument("--prune", action="store_true",
                    help="remove generator-owned .toml files in the target that no catalog entry claims")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written or removed, and change nothing")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.check:
        return check()
    if args.update_golden:
        return update_golden()
    # default is --apply (an explicit write, but never outside the clone unless asked)
    return apply(args.workspace, args.install_to, args.out, args.prune, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
