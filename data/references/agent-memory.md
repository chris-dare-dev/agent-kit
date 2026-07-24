---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Agent Memory Architecture

Per-subagent persistent memory for the milestone pipeline and other Claude Code agent workflows.

> **Unified root / consolidation:** agent-memory is fragmented across ~44 cwd-relative
> `.claude/agent-memory` roots. The consolidation to ONE unified root (via a symlink-farm + the
> `$AGENT_MEMORY_ROOT` resolution order, with NO agent-body edits) is documented in
> [`agent-memory-consolidation.md`](agent-memory-consolidation.md) (self-improving-tooling-m2 / E2).

## Why `memory: project`

The `memory: project` frontmatter field is documented in third-party Claude Code references (see the user's quoted advice in the milestone-pipeline conversion PR, 2026-05-17) as giving each agent a persistent `.claude/agent-memory/<name>/` directory across runs. **As of 2026-05-17, this MCP server's discovery code (`src/discovery/agents.ts`) does not introspect the `memory:` field — it passes the raw frontmatter through to whatever consumer reads it.** Whether the Claude Code CLI honors this field at agent-dispatch time depends on the CLI version; if it does not, the agent body MUST handle file persistence manually via `mkdir -p .claude/agent-memory/<name>` and `>> .claude/agent-memory/<name>/lessons.md` from within the agent's bash blocks (which is exactly what the four milestone-pipeline subagents do today — see their "Memory protocol" sections).

**Net effect:** the file-IO pattern in the agent bodies is the load-bearing mechanism. The `memory: project` frontmatter is a hint that future Claude Code CLI versions may honor by ensuring the directory is writable / non-ephemeral; today's pipeline does not depend on that hint. If you write a new agent that uses `memory: project`, also include the explicit `cat`/`>>` pattern in the body.

This memory is distinct from:

- **Main-session context** — lost on compaction or restart
- **Workspace MEMORY.md** (`.claude/projects/-Users-chris-dare-Work-workspace/memory/MEMORY.md`) — user-level durable lessons across the entire workspace (tech debt, architecture decisions, global patterns)
- **Agent memory** — agent-specific, skill-specific lessons that make a particular agent more effective over time

The two layers are complementary, not competitive:
- **Workspace MEMORY.md**: "PQC EnvoyFilter trips sidecar-outbound TLS expectations — flag DR `tls: DISABLE` for sidecar-callers" (workspace-wide knowledge)
- **Milestone-adversary/lessons.md**: "On charts/istio-gateway/ milestones, the PQC finding fires ~3x per quarter — weight it HIGH even if the implementer claims it's handled" (agent-specific weighting heuristic)

## Convention: every milestone-pipeline subagent has a `lessons.md`

| Agent | Memory path | What to capture |
|---|---|---|
| `milestone-adversary` | `.claude/agent-memory/milestone-adversary/lessons.md` | Recurring false-positive patterns; findings that consistently catch real bugs; per-subsystem calibration |
| `milestone-rectifier` | `.claude/agent-memory/milestone-rectifier/lessons.md` | False-positive critic findings frequently invalidated; real-bug patterns critics miss; per-subsystem fix patterns |
| `milestone-researcher` | `.claude/agent-memory/milestone-researcher/lessons.md` | Prior-art sources that were invaluable; research dead-ends by domain area |
| `milestone-implementer` | `.claude/agent-memory/milestone-implementer/lessons.md` | Common CI failures by subsystem; formatting tool gotchas; Helm template rendering traps |

Optional additional files in each agent's memory dir:
- `failure-modes.md` — recurring traps that caused the agent to produce wrong output
- `links.md` — cross-references to other agents' memory or workspace MEMORY.md entries

## The READ-FIRST-WRITE-LAST protocol

Every agent that uses `memory: project` MUST follow this protocol:

**At task start:**
```bash
cat .claude/agent-memory/<name>/lessons.md 2>/dev/null || echo "(no lessons yet)"
```

Read the output before doing any work. If lessons exist, use them to:
- Calibrate attention (known-high-signal areas get more scrutiny)
- Avoid known false-positive re-investigation (skip or fast-verify)
- Check known-miss patterns explicitly (the ones critics predictably skip)

**At task end:**
```bash
mkdir -p .claude/agent-memory/<name>
echo "$(date -u +%Y-%m-%d) [{ID}] {one-line lesson}" >> .claude/agent-memory/<name>/lessons.md
```

Rules for the one-line lesson:
- Start with the date and milestone ID so entries are traceable
- Describe a PATTERN, not a one-time event ("Always check X when Y" not "X was wrong in this milestone")
- Do NOT duplicate an existing lesson — scan for near-matches before appending
- Lessons describing false positives are as valuable as lessons describing real bugs

## Size cap and self-consolidation

When `lessons.md` exceeds 500 lines, the agent should self-consolidate:

1. Read the full `lessons.md`
2. Group near-duplicate entries (same file, same pattern, same root cause)
3. Merge each group into one representative entry, keeping the most recent date
4. Overwrite `lessons.md` with the consolidated version
5. Verify line count is now < 200 (typical consolidation ratio is 3:1)

This prevents memory bloat from costing more tokens per task than the memory saves. A 500-line lessons.md at ~50 tokens/line = 25,000 tokens read on every task invocation — the consolidation pays back in the first post-consolidation task.

## Relationship to workspace MEMORY.md

```
~/.claude/projects/-Users-chris-dare-Work-workspace/memory/MEMORY.md
  ↑ Human-curated, workspace-wide, long-lived
  ↑ Entries reference-tagged (e.g. [PQC EnvoyFilter constraints])
  ↑ Updated by the main session, not subagents

.claude/agent-memory/<name>/lessons.md
  ↑ Agent-specific, auto-updated by the agent at task end
  ↑ Captures recurrence frequency and calibration heuristics
  ↑ Can reference MEMORY.md entries: "see [PQC EnvoyFilter constraints] in MEMORY.md"
```

When an agent lesson rises to workspace-wide significance (applies to all agents, all milestones), it should be promoted to MEMORY.md by the user or main session. Example promotion path:
- Adversary agent lesson: "Charts/istio-gateway/ IRSA findings are real 90% of the time"
- After 5 confirmations → promote to MEMORY.md as reference_irsa_istio_gateway_signal

## The wiki tier: shared, cross-agent, human-browsable Field Notes

As of `wiki-mcp-readonly-m1` + `wiki-writeback-m2`, each `platform/source/<repo>` has a
GitLab wiki reachable **on demand** via three MCP tools (never eager-loaded — token-safety
measured in `wiki-mcp-readonly-spike-1`):

| Tool | Purpose |
|---|---|
| `get_repo_wiki({repo, page})` | fetch one wiki page (size-capped) |
| `search_repo_wiki({repo, query})` | top-k bounded snippets |
| `append_repo_field_note({repo, entry, ...})` | **GATED** write — append a Field Note |

This is a FOURTH memory location. To stop it becoming a drifting source of truth (the
capability-scout internal-adversary's central warning), the layers are ordered by a
**promotion flow** — they are NOT written identically:

```
.claude/agent-memory/<agent>/lessons.md     per-agent, local, gitignored      (CAPTURE)
        │  promote when the lesson is durable AND useful to OTHER agents in this repo
        ▼
<repo> wiki  →  Field Notes                 shared, cross-agent, human-browsable (SHARE)
        │  promote when the finding is validated + canonical for the repo
        ▼
<repo>/CLAUDE.md  (Decision Records/Pitfalls) canonical, auto-loaded, code-reviewed (CANONICAL)
        │  promote when it applies across repos
        ▼
data/references/*.md                         cross-repo, search_platform_knowledge (GLOBAL)
```

### The memory-write protocol (the rule)

When an agent makes a **significant, durable** memory change in a repo:

1. It already appends to its local `lessons.md` (READ-FIRST-WRITE-LAST, above).
2. If the lesson is **cross-agent** (useful to OTHER agents in this repo), promote it to
   the repo's wiki Field Notes — via **`/memory-sync`** (which stages a gated
   `append_repo_field_note` dry_run → human approves the preview → confirm).
3. If it is **canonical** (a durable invariant/decision), promote it into the repo's
   `CLAUDE.md`.
4. If it is **cross-repo**, graduate it to `data/references/` (commit + push).

**Do NOT triple-write the same content** — each tier is a distinct altitude. Keyed on
*significance*, not "always": routine per-run calibration stays in `lessons.md`.

**Gate.** Wiki writes are GitLab writes → the External Write Policy applies. A sub-agent
cannot self-approve; `append_repo_field_note` enforces a dry-run→confirm-token round-trip
(single-use, params-bound, TTL-limited) and a human approves the preview. The
human-in-the-loop guarantee is a *policy* guarantee enforced by the orchestrator surfacing
the preview — not a cryptographic one. Runtime prereqs: `GITLAB_WRITE_TOKEN` (api scope)
+ `WIKI_CONFIRMATION_KEY`; absent either, the write tool is inert.

**Never eager-load the wiki.** Measured: eager-loading wiki content into every in-repo
session breaches the token-bloat gate (+40–200% over time); on-demand retrieval adds ~0
baseline. Read the wiki only when relevant — `search_repo_wiki` first, `get_repo_wiki` for
a specific page.

## Reference pattern: milestone-pipeline

The `milestone-pipeline` slash command (`data/commands/milestone-pipeline.md`) is the canonical example of the slash-command + subagents + agent-memory pattern. The four subagents are:

- `milestone-researcher` (Agent A) — Phase 1, parallel
- `milestone-implementer` (Agent A) — Phase 2, delegated path
- `milestone-adversary` (Agent B = this repo) — Phase 3, parallel fan-out
- `milestone-rectifier` (Agent B = this repo) — Phase 4, dispatched or main-session

All four use `memory: project`. Over 10+ milestones, each agent's `lessons.md` builds a calibrated knowledge base that makes the pipeline increasingly accurate and efficient.

Future complex pipelines should follow the same pattern: slash command as orchestrator, subagents as workers, `memory: project` on each subagent for continuous improvement.
