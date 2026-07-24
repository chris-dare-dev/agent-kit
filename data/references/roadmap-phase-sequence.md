---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Phase 3 — SEQUENCE

**Goal:** order epics into Now / Next / Later lanes, decompose Now-lane epics into stories ≤ 3 days each, assign milestone IDs compatible with `/milestone-pipeline`, and surface a discovery/spike track for unvalidated assumptions.

## Step 1 — MoSCoW (cut-line)

For each epic, pick a bucket:

| Bucket | Definition | Cap |
|---|---|---|
| **Must** | Failing to deliver this means the goal fails. | ≤ 60% of total |
| **Should** | Important but the goal still partially succeeds without it. | ~20–30% |
| **Could** | Nice-to-have; deliver only if Musts/Shoulds finish early. | ~10–20% |
| **Won't** | Explicit non-goal for this roadmap horizon. Adds to the Phase 1 Won't list. | unbounded |

Run the script to validate:

```bash
echo '[{"id":"E1","bucket":"Must"}, ...]' | python3 data/scripts/roadmap-score-moscow.py -
```

Exit code 0 if Musts ≤ 60%; exit code 1 with a warning if violated. **If violated, re-bucket** — do NOT proceed. The most common cause is conflating "I want this" with "the goal needs this". Push every Must through the test: "if we shipped without this, would the goal fail?" If "no", it's a Should.

## Step 2 — RICE (rank the Musts)

For the Musts ONLY, score:

| Variable | Scale | Definition |
|---|---|---|
| **R**each | 1–10 | How many users / systems / engineers this benefits per quarter. (Platform: number of clusters or tenants affected.) |
| **I**mpact | 1–10 | Magnitude per beneficiary. 10 = makes-or-breaks; 5 = noticeable improvement; 1 = marginal. |
| **C**onfidence | 1–10 (or 0.1–1.0) | How sure we are about R and I. **Default to 5 (50%) when there's no evidence.** Score above 7 only with data. |
| **E**ffort | 1–10 | Person-weeks. (1 = ≤ 1 week; 5 = ~1 month; 10 = quarter.) |

RICE = R × I × C ÷ E. Higher = higher priority within the Must lane.

Run the script:

```bash
echo '[{"id":"E1","reach":8,"impact":7,"confidence":5,"effort":3}, ...]' | python3 data/scripts/roadmap-score-rice.py -
```

Input JSON format: `[{"id": "E1", "reach": 8, "impact": 7, "confidence": 5, "effort": 3}, ...]`. Output: ranked table with RICE scores, printed to stdout.

**Don't RICE the Shoulds and Coulds.** Their order is set by Now-lane capacity overflow, not by score.

## Step 3 — Now / Next / Later assignment

| Lane | Contents | Detail level |
|---|---|---|
| **Now** | Highest-RICE Musts that fit team capacity (default 1–3 epics, ≤ 6 weeks combined for a 1–3 person platform team). | Stories ≤ 3 days each, full Given/When/Then AC. |
| **Next** | Remaining Musts + top Shoulds. | Shaped (epic-level AC), no story-level AC yet. |
| **Later** | Rest of Shoulds + Coulds. | Outcome only — no solutions, no decomposition. |

**Detail decays with horizon.** Date-committing a Later item is planning theatre. The roadmap is **rolling-wave**: as the team finishes Now items, Next items are pulled forward and decomposed.

## Step 4 — Now-lane decomposition

For each Now-lane epic, write stories.

Each story:
- **Title** — action verb, no conventional commit prefix.
- **Size** — XS (≤ 1 day) / S (1–2 days) / M (2–3 days). **Cap at M.** If larger, split using SPIDR (Spike / Path / Interface / Data / Rules).
- **AC (Given/When/Then)** — 1–3 per story; > 3 means the story is too big.
- **DoD** — universal: code reviewed, tests pass, deployed to dev, docs updated, observability in place. The roadmap doesn't repeat DoD per story — it lives once at the top of the file.
- **External writes required** — list explicitly (per workspace CLAUDE.md). Becomes the input to `/milestone-pipeline` Phase 4.

INVEST check at the story level:
- **I**ndependent — runs in any order within the epic.
- **N**egotiable — implementation is open.
- **V**aluable — visible outcome.
- **E**stimable — team can size with confidence.
- **S**mall — ≤ M = ≤ 3 days.
- **T**estable — observable AC.

## Step 5 — Milestone IDs

Each Now-lane epic becomes a milestone with ID format: `<slug>-m<N>` where `<slug>` is the roadmap slug (from Step 0) and `<N>` is 1-indexed.

Examples:
- `cost-visibility-l3-m1`
- `kiali-multicluster-tenant-acme-m1`
- `keycloak-26-migration-m2`

These IDs are designed to be passed directly to `/milestone-pipeline <id>`. The roadmap doc cross-references each milestone ID with its `/milestone-pipeline` invocation in the Phase 4 cross-references section.

## Step 6 — Spike / discovery lane

Every roadmap MUST have a spike lane. A spike is a time-boxed investigation whose output is a decision document, NOT shipped code.

Triggers for a spike epic:
- Any `[MUST]` assumption from Phase 1 that wasn't validated from in-context evidence.
- Any epic that failed INVEST's Estimable check ("no idea, need to look").
- Any cross-cutting decision that needs upstream validation (vendor docs, OSS license, breaking change).

Spike rules:
- Time-box ≤ 3 days.
- Output: a decision doc at `<repo>/plans/<spike-id>-decision.md` with options + recommendation + rejected alternatives.
- Spike precedes its dependent epic in Now or Next.
- Spike has NO Acceptance Criteria — only an "Output: decision doc with X" line.

Without spikes, every estimate beyond the spike's question is a lie.

## Phase 3 output template (writes to the `## Roadmap — Now / Next / Later` section)

```markdown
## Roadmap — Now / Next / Later

### Now (≤ 6 weeks combined)

#### M1: <Title> — milestone ID `<slug>-m1`

- [ ] **Status:** pending   <!-- tick `- [x]` when done · `- [/]` in progress — drives the live roadmap status boards (workspace CLAUDE.md "Roadmap milestone status"). KEEP this `- [ ]` line directly under the heading. -->
- **Source epic:** E1
- **MoSCoW:** Must
- **RICE:** R8 × I7 × C5 ÷ E3 = 93 (rank #1)
- **Specialist:** service-mesh
- **Stories:**
  - **S1.1:** <story title> — Size S
    - Given <context> When <action> Then <observable outcome>
    - Given <context> When <action> Then <observable outcome>
    - External writes required: `kubectl apply -f ...`, `git push origin HEAD:main (charts/kiali)`
  - **S1.2:** <story title> — Size M
    - …
- **Run with:** `/milestone-pipeline <slug>-m1`

#### M2: <Title> — milestone ID `<slug>-m2`
…

### Next (shaped, no story-level AC)

- **E3:** <Title> — Must, RICE rank #4. Epic-level AC carried from Phase 2.
- **E4:** <Title> — Should. Pull forward when Now lane capacity opens.

### Later (outcomes only)

- **E5:** <Title> — Should. Outcome: <KR-shaped sentence>.
- **E6:** <Title> — Could. Outcome: <KR-shaped sentence>.

### Spike / discovery lane

- [ ] **SP1:** Validate IRSA OIDC trust on tenant-acme cluster. ≤ 3 days. Output: `plans/sp1-irsa-acme-decision.md`. Blocks M1.
- [ ] **SP2:** Confirm Keycloak v26 realm-import schema migration path. ≤ 2 days. Output: `plans/sp2-keycloak26-decision.md`. Blocks M2.

> **Format is load-bearing.** The milestone heading (`#### M<n>: … — milestone ID \`<slug>-m<n>\``), the `- [ ]` status line directly beneath it, and the spike checkboxes are parsed by the live roadmap status boards (`scripts/roadmap_status_excalidraw.py`). Do not invent ad-hoc milestone formats (bold-backtick slugs, `## Milestone m<n>` headings, status buried in prose) — they render inconsistently and may not be tracked. One milestone heading + one `- [ ]` status line.

### Won't (cut from this roadmap horizon)

- E7 (Per-pod cost attribution): cut — namespace granularity is the bar (per Phase 1 Won't).
- E8 (Cross-account cost attribution): cut — stage/prod accounts deleted.
```

## Auto-advance vs gate (this phase)

| Condition | Action |
|---|---|
| RICE ranking is unambiguous (no two Musts within 10% of each other for the cut-line); Must vs Should split has no contested items (cap held); team-capacity assignment to Now lane is straightforward | Auto-advance to Phase 4 |
| Two Musts have RICE scores within 10% AND only one fits in Now-lane capacity | GATE — present both with their scores + downstream consequences, ask user to break the tie |
| MoSCoW Must cap (60%) is violated even after re-bucket attempts | GATE — surface, ask whether to accept (with explicit scope rationale) or downgrade items |
| The Now lane has zero `value` epics (all enabler) | GATE — flag as anti-pattern; ask whether to accept or pull a value epic forward |
| User explicitly asked for a checkpoint | GATE |

When gating, present the contested epics + RICE scores + downstream consequences. Accept user direction; integrate; proceed.

## Hard rules

- **Cap Musts at 60%.** Script enforces. Re-bucket if violated.
- **RICE the Musts only.** Shoulds and Coulds are ordered by capacity overflow.
- **Stories cap at M (≤ 3 days).** Beyond that, split via SPIDR.
- **Now-lane stories have full Given/When/Then AC.** Next is shaped; Later is outcome-only.
- **Every Now milestone has an ID** of the form `<slug>-m<N>` for `/milestone-pipeline` consumption.
- **Every roadmap has a spike lane.** Even if empty, document why ("all assumptions validated from in-context evidence").
- **List external writes required per story.** Phase 4 of `/milestone-pipeline` will use this for user authorization.
- **Don't decompose Next or Later epics into stories.** Premature; wastes effort.
