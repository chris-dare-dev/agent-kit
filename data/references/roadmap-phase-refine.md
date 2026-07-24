---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Phase 1 — REFINE

**Goal:** convert a vague brief (or conversation context) into a 1-page goal statement with explicit Objective, Key Results, assumptions, and Won't list. The output of this phase is what every subsequent phase plans against.

## Step 1 — "How Might We" reframe

Restate the brief as a single sentence: `How might we <verb> <outcome> for <who>?`

- The verb describes a desired *outcome*, not a planned *solution*.
- "How might we add a Grafana dashboard" → wrong (solution-embedded).
- "How might we make per-namespace cost visible to platform engineers" → right (outcome-shaped).
- "How might we" specifically — not "How will we" or "How do we" — keeps the door open to multiple solutions.

Anti-pattern: solution-embedded HMWs lock in the answer before the work starts. If the verb is the solution (`add`, `migrate`, `deploy`), challenge whether that's truly required or whether it's an assumed implementation.

## Step 2 — Sharpening questions (3–5, not more)

Pick the questions that are genuinely under-specified. Don't ask all of them; ask the ones that change the plan.

| Lens | Question | When to ask |
|---|---|---|
| Who | Who is this for? Which user / role / system? | Always |
| Success | What does success look like in 1 line? | Always |
| Constraints | What's the deadline / budget / org constraint? | If timing matters |
| Prior art | What already exists in the repo / industry that solves this? | Always — drives Phase 1 in-codebase scan |
| Why now | Why now and not 6 months ago / 6 months from now? | If the timing isn't obvious |
| Definition of "good enough" | What's the smallest version that's still valuable? | If the brief is grandiose |
| Stakeholders | Who else cares / approves / is affected? | If cross-team |

For each question, answer from in-context evidence FIRST:
- Workspace + repo CLAUDE.md
- the platform MCP (`mcp__agent-kit__search_platform_knowledge`, `get_app_context`, `get_context_guide`, `get_ops_reference`, `get_environment_map`)
- Your own auto-memory (optional — empty on a fresh machine): `mcp__agent-kit__search_memory`
- Repo grep for related modules / charts / overlays
- The current conversation transcript (the orchestrator's own context)
- Confluence (if a design doc reference seems likely)

If none of the above answer a question, mark it as an assumption (Step 3) and move on. Do NOT block on the user for every question — only block on questions whose answers would invalidate the plan (those become `[MUST]` assumptions).

## Step 3 — Assumption tiers

Every load-bearing belief gets explicitly tagged:

| Tier | Meaning | Action |
|---|---|---|
| `[MUST]` | Must be true, or the plan is invalid | Validate before Phase 2 (read code, ask user, run a spike). If unvalidated, BLOCK Phase 2. |
| `[SHOULD]` | Should be true, or the plan degrades | Design a fallback. Note in the roadmap. |
| `[MIGHT]` | Might be true; non-load-bearing | Defer. Revisit at Phase 3 cut-line if it affects priority. |

Examples:
- `[MUST]` Tenant-acme cluster has IRSA OIDC trust configured on the new role naming pattern → if false, Phase 2 epic split is wrong.
- `[SHOULD]` We'll have 2 weeks of stage soak time before promotion → if false, design a faster-rollback path.
- `[MIGHT]` The team will adopt the same dashboard for tenant-example → if false, the per-tenant dashboard work is still useful.

Tier-down vigorously. Most "Musts" can be reframed as "Shoulds" with a fallback. Only true binary make-or-break items deserve `[MUST]`.

## Step 4 — Objective + 2–4 Key Results

The Objective is qualitative direction; the KRs are quantitative outcomes that measure the Objective.

| Construct | Shape | Example |
|---|---|---|
| Objective | Inspirational, time-bound, qualitative | "Make per-namespace cost visible and actionable for platform engineers by Q3." |
| Key Result | Measurable outcome, 3–5 per Objective; outcome not output | "≥ 80% of tenant namespaces report cost in the cost-visibility dashboard" |

**Decision rules for KRs:**

1. **Measure the Objective even if the planned work fails.** A KR that says "ship the dashboard" is an output, not an outcome — replace with "the dashboard is used by ≥ 5 distinct engineers per week".
2. **Engineering KRs lean SLO/DORA-shaped:** lead time, deploy frequency, change-failure rate, MTTR, p95 latency, error budget burn. These are the canonical scaffold.
3. **3–5 KRs per Objective.** Fewer = ambiguous; more = unfocused.
4. **70% attainment is success for an OKR.** If 100% feels achievable, the KR isn't ambitious enough. (For SMART goals — used at the story level — 100% is the bar.)
5. **Time-bound the KR explicitly:** "by end of Q3", "within 30 days of GA", "in dev env first then promotion gate".

Anti-pattern: rephrasing the initiative as the KR. "Migrate Keycloak to v26" is an initiative; "Keycloak v26 in prod with zero auth incidents over the 30-day window post-cut" is a KR.

## Step 5 — The Won't list

Explicit non-goals. Without a Won't, every priority discussion restarts.

Format: bulleted list, each item is one line, justifying-rationale optional.

```markdown
## Won't (explicit non-goals)

- Cross-account cost attribution. (Stage/prod accounts deleted; not in scope.)
- Per-pod cost — namespace granularity is the bar.
- Backfill historical cost data > 30 days.
- Custom UI changes; reuse the existing admin-web-app cost panel.
```

The Won't list is loadbearing for Phase 3 — it's the input to "what do we cut" decisions.

## Phase 1 output template (writes to the `## Goal` section of the roadmap doc)

```markdown
## Goal

**How might we:** <HMW sentence>

**Objective:** <one sentence, qualitative, time-bound>

**Key Results:**
- KR1: <measurable outcome, time-bound>
- KR2: <measurable outcome, time-bound>
- KR3: <measurable outcome, time-bound>

**Assumptions:**
- `[MUST]` <assumption>. Validation: <how / who>.
- `[SHOULD]` <assumption>. Fallback: <plan if false>.
- `[MIGHT]` <assumption>. Revisit at Phase 3.

**Won't (explicit non-goals):**
- <non-goal>
- <non-goal>

**In-context evidence reviewed:**
- <CLAUDE.md / MCP tool used / file path>
- <workspace memory entry referenced>
- <Confluence page if any>
```

## Auto-advance vs gate (this phase)

| Condition | Action |
|---|---|
| Brief was specific; sharpening questions answered from in-context evidence; no `[MUST]` assumptions need user validation | Auto-advance to Phase 2 |
| Brief was vague AND ≥ 2 credible interpretations exist | GATE — present interpretations, ask user to pick |
| ≥ 1 `[MUST]` assumption needs user validation (and isn't validatable from in-context evidence) | GATE — list the assumptions, ask user to confirm/correct |
| User explicitly asked for a checkpoint | GATE |

When gating, present the goal-statement draft + the specific question(s); accept short user answers, integrate, and proceed.

## Hard rules

- **Don't write code in Phase 1.** Phase 1 output is a goal statement.
- **Don't ask all 7 sharpening questions.** Pick the 3–5 that change the plan.
- **Don't skip the Won't list.** It's not optional.
- **Don't accept output-shaped KRs** ("ship X", "deploy Y"). Reject and rewrite as outcome-shaped.
- **Don't gate on every assumption.** Only on `[MUST]` ones that need user input. The rest are flagged in the doc.
