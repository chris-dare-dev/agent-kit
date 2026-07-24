---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Model + Effort Policy — central registry for all pipeline agents

**Registry:** `data/model-policy.json` · **Stamper:** `data/scripts/model-policy-apply.py` · **Owner keys:** `model-class:`, `model:`, `effort:` in every `data/agents/*.md`

One JSON file governs which model and effort level every one of the 78 agents runs at. Agents carry a semantic **`model-class:`** key (the placeholder — names the *role tier*, not a model); the apply script materializes each class's current resolution into the `model:` / `effort:` keys the Claude Code runtime reads. Command prose that needs a dispatch-time value embeds a marker span the script keeps stamped:

```
<!-- {{model-policy:deep-reasoning-high}} -->fable (effort: high)<!-- /model-policy -->
```

## The class vocabulary

| Class | Resolves to (today — cells are script-stamped) | Count | Who |
|---|---|---|---|
| `deep-reasoning-max` | <!-- {{model-policy:deep-reasoning-max}} -->fable (effort: max)<!-- /model-policy --> | 12 | Every Phase-3 challenger/reviewer, milestone-adversary, milestone-infra-safety, spike-reviewer — the gates. Errors here let bad work ship; effort here steers everything upstream. (milestone-frontend-ux is a deliberate `balanced-high` exception: advisory, not gating.) |
| `deep-reasoning-high` | <!-- {{model-policy:deep-reasoning-high}} -->fable (effort: high)<!-- /model-policy --> | 9 | Implementers, rectifier, argoops-remediator, roadmap planners (refiner/decomposer/sequencer), spike-designer, builders' authors — output IS the deliverable. |
| `balanced-high` | <!-- {{model-policy:balanced-high}} -->sonnet (effort: high)<!-- /model-policy --> | 35 | All discovery scouts, researchers, diagnosticians, cartographers — breadth+judgment in parallel multiples; the dominant token spend, so Sonnet is the cost/accuracy sweet spot. |
| `balanced-standard` | <!-- {{model-policy:balanced-standard}} -->sonnet (effort: medium)<!-- /model-policy --> | 1 | roadmap-materializer — template-following lint + drafts. |
| `fast-mechanical` | <!-- {{model-policy:fast-mechanical}} -->haiku<!-- /model-policy --> (no effort key — Haiku has no effort levels) | 2 | argoops-triage, spike-writer — deterministic scripts do the logic; the agent transcribes. Both are safe-by-architecture: argoops routing rides the script-produced classification.json the orchestrator reads itself, and the spike verdict is independently re-derived by the fable-max reviewer. |
| `specialist-default` | <!-- {{model-policy:specialist-default}} -->sonnet (effort: high)<!-- /model-policy --> | 17 | Standalone ad-hoc specialists (helm-apps, gitops, node-ops, …). Separate knob from `balanced-high` so specialists can be re-pointed independently of pipeline scouts. Known change: `context-curator` previously had no `model:` key (inherited the session model) and is now pinned — deliberate. |

Design principle (lock-in 2026-06-10): **adversaries/critics run at least one tier above the agents they critique; planners get frontier reasoning; collectors get the smallest model that can transcribe deterministic script output.**

### Reserved (data-gated) entries

`data/model-policy.json` carries an optional top-level `reserved` map for agents that are
**approved-in-principle but must not exist yet** (no agent file, no assignment — an
orphaned assignment would fail `--check` with exit 4). Each entry records the pre-agreed
class, the evidence gate that must be met before creation, and the promotion recipe.
Current: `milestone-research-challenger` (`deep-reasoning-max`, gated on outcomes.jsonl
evidence of research-brief invalidation/rework — placeholder approved 2026-07-09). The
apply script ignores `reserved`; it is documentation with a schema, kept next to the
assignments it will graduate into.

## Re-pointing the fleet (e.g. model alias repointing)

1. Edit the class resolution(s) in `data/model-policy.json` (e.g. `"deep-reasoning-max": {"model": "opus", "effort": "max", ...}`). Each class lists `fallbacks` documenting the intended repoint order.
2. `python3 data/scripts/model-policy-apply.py` — restamps all agent frontmatter + command marker spans.
3. Review `git diff`, commit, and push directly to `origin/main` (push is gated by the External Write Policy).

`--check` exits 2 on drift / 3 on lint (hardcoded model literals in command prose) — run it before committing anything under `data/`. **Never hand-edit `model:` / `effort:` / `model-class:` in agent files or marker-span contents** — the script owns them and will overwrite.

## Rules for orchestrators (slash commands)

- **Default dispatch: pass NO model override.** The agent's stamped frontmatter governs. (Verified precedence: agent frontmatter `model:` > `CLAUDE_CODE_SUBAGENT_MODEL` env > session model.)
- **Mode bumps (`--mode deep`, `--executor deep`):** pass ONLY the model token from the stamped marker span in the command's Arguments section (e.g. from `fable (effort: high)` pass `model: fable`). Effort cannot be passed per-dispatch — it rides the agent's frontmatter.
- **Incident escalation for cluster-mutating specialists** (`argocd-ops`, `node-ops`, `platform-infra`, `service-mesh`): their pinned `specialist-default` beats even a frontier session's model, so when an incident warrants frontier reasoning, pass a dispatch-time model override (same mechanism as the deep bumps) — that is the sanctioned escalation path; do not hand-edit their frontmatter. `service-mesh` is the standing promotion candidate if this recurs.
- **New agents:** add the agent to `assignments` in the registry, run the apply script. The script exits 4 if an agent file exists with no assignment (or vice versa), so a missing assignment is caught on the next run.
- **Hardcoded model literals in command prose are lint violations.** Use a class name, or a marker span when the resolved value is needed inline. Suppress a deliberate literal with `<!-- model-policy:ok -->` on the same line.

## Effort facts (verified against code.claude.com/docs + empirically, 2026-06; repointed to Fable 2026-07-09)

- Effort levels: `low | medium | high | xhigh | max`. Fable 5 and Opus 4.8/4.7 support all; Opus/Sonnet 4.6 support low/medium/high/max; **Haiku supports none** (the policy stamps no effort key on `fast-mechanical`).
- Unsupported level on the active model → runs the highest supported level at-or-below the request (so `max` on a Sonnet fallback degrades to Sonnet-max/high gracefully).
- Agent frontmatter `effort:` is documented to apply to subagents (overrides session effort; default inherits). **VERIFIED 2026-06-14 (controlled probe + ~1,964 historical subagent transcripts; see memory `effort-key-inert-for-subagents`): it does NOT deliver the headline `max`-vs-`high` reasoning premium on Opus.** Cause is **adaptive reasoning** — Opus 4.7+/4.8 decide *per step* whether to emit extended thinking; `effort: max` lifts the token *cap* (raises the ceiling) but does **not force** thinking, so on steps the model judges easy it emits none. Observed extended-thinking-block rate in subagent transcripts: **opus ~1%, sonnet 0%, fable 99%**. Adaptive reasoning **cannot be disabled** on Opus 4.7+ (`CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING` / fixed-budget mode do not apply); Fable-5's thinking is **mandatory**, which is exactly why the `deep-reasoning-max`/`deep-reasoning-high` classes were repointed back to `fable` on 2026-07-09 (Fable had gone temporarily unavailable; the classes ran on `opus` from 2026-06-13 until then, silently losing the reasoning premium the two classes exist to buy). `MAX_THINKING_TOKENS` only *reduces* thinking (=0 turns it off except on Fable); it is not a force-on lever for Opus. The `ultrathink` keyword placed in a **subagent prompt** was tested and did **not** trigger opus thinking (it is a main-session feature). **Net: on the current `fable` resolution the `deep-reasoning-max` vs `deep-reasoning-high` effort distinction is secondary — Fable thinks regardless of effort level, so the premium these classes buy comes from the model choice, not the effort tier. If a future repoint ever falls back to `opus`/`sonnet` (entitlement loss), re-read this note: the reasoning premium degrades silently, not loudly.**
- **To increase a gate's reasoning regardless of model:** the controllable, model-agnostic lever is **prompt-engineered VISIBLE deliberation** — instruct the high-stakes agent to write a full, structured analysis BEFORE its verdict (this also makes the reasoning auditable in the transcript, which the ephemeral thinking channel is not). Secondary: keep the highest-stakes reasoning in the MAIN session (which always thinks). With `fable` restored as the primary resolution this is a belt-and-braces addition, not the only lever. (Cost note: Fable's mandatory thinking means `deep-reasoning-*` agents are no longer cost-neutral the way the interim `opus` resolution was — budget accordingly.)
- **Entitlement caveat:** `fable` requires Fable 5 access (Max plan / API entitlement). A teammate without it will hard-fail at dispatch on any deep-reasoning agent. Team fallback = the one-line JSON repoint (step list above) to `opus` or `sonnet` — each class's `fallbacks` array documents the order.
- **Do NOT set `CLAUDE_CODE_EFFORT_LEVEL`** in shared settings — it overrides *every* surface including per-agent frontmatter, which defeats this policy.
- Aliases (`fable`/`opus`/`sonnet`/`haiku`) resolve at runtime to the latest model per provider — the registry intentionally uses aliases, never pinned IDs, so provider-side upgrades flow automatically. Pin via `ANTHROPIC_DEFAULT_*_MODEL` env vars only as an emergency per-machine override.

## Per-pipeline optimized chains (accuracy ↑ where it gates, cost ↓ where it fans out)

| Pipeline | Fan-out (cost center) | Gate (accuracy center) |
|---|---|---|
| /milestone-pipeline | 2× researcher `balanced-high` | implementer `deep-reasoning-high`; adversary + infra-safety `deep-reasoning-max` |
| /argoops | triage `fast-mechanical`; 3 diagnosticians `balanced-high` | challenger `deep-reasoning-max`; remediator `deep-reasoning-high` |
| /roadmap | — (sequential) | refiner/decomposer/sequencer `deep-reasoning-high`; materializer `balanced-standard` |
| /spike | executor `balanced-high` (`--executor deep` bumps) | designer `deep-reasoning-high`; writer `fast-mechanical`; reviewer `deep-reasoning-max` |
| discovery pipelines (capability/cicd/interop/mesh/frontend/zerotrust) | 3–5 scouts `balanced-high` | challenger `deep-reasoning-max`; synthesis stays in the main session |
| builders (pipeline-builder, skill-to-command) | authors `deep-reasoning-high` | reviewers `deep-reasoning-max` |

## Per-provider resolution maps (S3.8a + M3-packs)

Each `classes.<name>` entry carries a `providers` block resolving the class to a
concrete model (and, for Codex, a reasoning effort) for each provider the agent-kit
targets. The adapter packs (`generate-adapter-packs.py`) consume these:

```json
"providers": {
  "claude":   { "model": "fable", "effort": "high" },
  "codex":    { "model": "gpt-5.6", "effort": "high" },
  "opencode": { "model": "amazon-bedrock/us.anthropic.claude-opus-4-8" }
}
```

- **`claude`** MUST equal the class's own `model` + `effort` — the class is the
  single source of truth; the equality is enforced by
  `data/scripts/model-policy-providers-check.py` (wired into the `data-lint` CI
  job). When you re-point a class, update `providers.claude` in the same edit.
- **Codex `effort` IS set** (M3-packs adapter pass): `deep-reasoning-max`→`xhigh`,
  `deep-reasoning-high`/`balanced-high`/`specialist-default`→`high`,
  `balanced-standard`→`medium`, `fast-mechanical`→omitted. These are exactly the
  `model_reasoning_effort` values the hand-made 70 `.codex/agents/*.toml` already
  used, so the generated Codex pack preserves live behavior. **The Codex `.toml`
  carries only `model_reasoning_effort`, never a `model` field** (Codex picks the
  model globally via config/profile), so `codex.model` (`gpt-5.4/5.5/5.6`) is
  informational and is NOT stamped into any pack — `gpt-5.6`'s unverified status
  (`gpt-5.6-sol` may be the live pin) therefore never reaches a stamped artifact.
- **OpenCode `model` IS stamped** into `.opencode/agents/*.md`, so it is the one
  string that must be live-correct. **Verified 2026-07-11** via
  `aws bedrock list-inference-profiles` (dev account): `us.anthropic.claude-opus-4-8`
  and `us.anthropic.claude-sonnet-5` exist date-less (as `platform/opencode.json`
  uses them); the shape-conformed date-less `us.anthropic.claude-haiku-4-5` does
  **NOT** exist — the only Haiku 4.5 profile is the dated
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`, which `fast-mechanical.opencode`
  was corrected to. (The newest models 4-6/4-7/4-8/5 got clean aliases; haiku-4-5
  did not — always verify before pinning a Bedrock ID.)

When adding a class, populate all three provider maps or the CI gate fails.

## Workflow raw-alias lint + `--mode deep` variants (M3-packs / S3.8b)

- The `lint` block adds `workflow_glob` (`data/scripts/*-workflow.mjs`) and
  `workflow_forbidden_patterns` — quoted model-alias literals (`'opus'`, `` `opus` ``,
  `'opus-4-8'`, …) in a workflow file fail `model-policy-apply.py --check`. A model
  tier is a policy concern, never a literal in a Gen-2 workflow (the Workflow runtime
  has no filesystem access to resolve the policy).
- A `--mode deep` scout tier bump lives in a dedicated **per-mode agent file**
  (`data/agents/<base>-deep.md`, `model-class: deep-reasoning-high`, `catalog-exclude: true`),
  generated + drift-gated by `data/scripts/mode-variant-agents.py`; the workflow
  dispatches `agentType: '<base>-deep'`. The `-deep` files carry `catalog-exclude`
  so they stay out of the public catalog and the Codex/OpenCode packs (those providers
  cannot run the Claude-Workflow that dispatches them) — but they DO get a
  `model-policy.json` assignment and are stamped like every other agent.
