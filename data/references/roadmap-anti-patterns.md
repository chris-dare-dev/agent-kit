---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Roadmap anti-patterns

12 ways a roadmap goes wrong, with rebuttals. Read this when something feels off but you can't articulate why. Each entry has a tempting-belief / reality / what-to-do triplet.

---

### 1. Outputs masquerading as outcomes

**Tempting belief:** "Ship the dashboard" is a perfectly good Key Result.
**Reality:** That's an output. The KR should measure whether the *Objective* moved — e.g. "≥ 80% of tenant namespaces report cost in the dashboard" or "≥ 5 distinct engineers use the dashboard per week". A KR that completes by virtue of shipping is just a deadline in disguise.
**What to do:** for every KR, ask "would this still be a meaningful measure if the planned implementation failed?" If no, rewrite as outcome-shaped.

### 2. Locked long horizons (Gantt theatre)

**Tempting belief:** "Stakeholders need dates for Q3."
**Reality:** Detail decays with horizon. Promising specific dates beyond the Now lane is fiction with a sign-off. The team will either hit the date by descoping silently (cost: trust + morale) or miss it (cost: trust + planning).
**What to do:** Now/Next/Later is the contract. Now is fully spec'd. Next is shaped, no story-level AC. Later is outcomes only. Date-committing a Later item is planning theatre — refuse.

### 3. Premature epic decomposition

**Tempting belief:** "Let's spec out all stories now so we don't have to later."
**Reality:** Stories written for Later epics get rewritten when the team learns more. The work is wasted, AND the early decomposition locks decisions that should stay open.
**What to do:** Story-decompose only Now-lane epics. Next is epic-level AC. Later is outcome only.

### 4. Missing discovery / spike track

**Tempting belief:** "We know enough to start; spikes are slow."
**Reality:** Without a spike lane, every estimate beyond a certain horizon is a lie. Unvalidated `[MUST]` assumptions become surprise CRITICALs in `/milestone-pipeline` Phase 3.
**What to do:** every roadmap has a spike lane (even if empty, document why). Spikes are time-boxed ≤ 3 days, output a decision doc, and precede their dependent epic.

### 5. Story-point inflation

**Tempting belief:** "An 8-pointer this quarter is the same as an 8-pointer last quarter."
**Reality:** Points drift up across quarters as the team hits unfamiliar work. Velocity becomes meaningless.
**What to do:** for senior platform teams, count items ≤ 3 days each rather than reasoning points. The script enforces story-size cap at M (≤ 3 days). T-shirts at the epic level are still useful for coarse capacity planning.

### 6. Planning theatre

**Tempting belief:** "RICE scores let us be objective."
**Reality:** RICE looks rigorous but R, I, and especially C are usually guesses. Treating the score as a decision is false precision. Three columns of made-up numbers don't beat one well-considered judgment.
**What to do:** RICE is a *forcing function* for explicit reasoning, not a decision oracle. Confidence defaults to 5 (50%); only score above 7 with evidence. Use RICE to break ties between Musts; don't use it to sequence Shoulds and Coulds.

### 7. All-Must MoSCoW

**Tempting belief:** "Everything in this roadmap matters; it's all Must."
**Reality:** If everything is Must, nothing is. The framework is meaningless and the cut-line decision is deferred (silently — to whoever's executing).
**What to do:** cap Musts at ≤ 60%. The script enforces this. Push every Must through "if we shipped without this, would the goal fail?" If "no", it's a Should.

### 8. Conflating milestone with epic

**Tempting belief:** "The milestone IS the epic."
**Reality:** A milestone is a *date* or *event* ("MVP", "GA", "tenant-acme cutover"); an epic is *work*. Putting work *in* a milestone (as if the milestone was the container) makes "is the milestone done" ambiguous.
**What to do:** epics contain stories. Milestones are checkpoints stamped on epics. A roadmap milestone ID (`<slug>-mN`) corresponds to one Now-lane epic; the milestone fires when the epic ships.

### 9. Horizontal-only slicing

**Tempting belief:** "We need a UI story, an API story, a DB story."
**Reality:** Each layer alone delivers no observable value. The user can't see the work until all layers ship together — and then they ship as a single unverifiable lump.
**What to do:** vertical-slice. One end-to-end path through all layers per story. Layer-by-layer is *tasks under a vertical-slice story*, not stories themselves. Enabler stories (foundation work) are explicitly tagged and time-boxed.

### 10. Definition of Ready missing

**Tempting belief:** "Stories enter the sprint and we figure out AC during the sprint."
**Reality:** Mid-sprint scope creep and "it depends what you mean by X" rework. The team commits to a fiction.
**What to do:** every Now-lane story has Given/When/Then AC and an explicit external-write list BEFORE entering Now. Stories without AC are still in Next (shaped, not Now).

### 11. No "Won't" list

**Tempting belief:** "We don't need to write down what we're NOT doing."
**Reality:** Every priority discussion restarts from zero. "What about feature X?" gets re-litigated each meeting. The cut-line decision is unstable.
**What to do:** explicit Won't list at Phase 1 (goal-level non-goals) and again at Phase 3 (epic-level cuts from this horizon). Justify briefly. The Won't is the *contract* — it shifts the conversation from "should we do X?" to "should we *re-add* X to the roadmap?"

### 12. OKR-as-task-list

**Tempting belief:** Each KR is a checkbox: "Migrate Keycloak", "Deploy dashboard", "Write runbook".
**Reality:** Those are initiatives — work the team will do. KRs are outcomes the work *moves*. The team can complete every initiative and miss every KR (the migration shipped, but auth incidents went up).
**What to do:** KRs measure the Objective. Initiatives are how. Both belong in the doc, in different sections. The roadmap's `## Goal` section has the Objective + KRs; `## Epics` and `## Now/Next/Later` have the initiatives.

---

## Quick-reference card

When you feel uncertain about a roadmap, walk these 12 in order:

| # | Smell | Fix |
|---|---|---|
| 1 | KRs say "ship X" | Rewrite as outcome |
| 2 | Later items have dates | Move to Next or strip dates |
| 3 | Stories written for Later | Delete; revisit at horizon |
| 4 | No spike lane | Add one (even if empty) |
| 5 | Reasoning in story points | Switch to ≤ 3-day cap |
| 6 | RICE scores treated as truth | Re-baseline confidence |
| 7 | > 60% Musts | Re-bucket |
| 8 | Milestone "contains" work | Restructure: epic contains stories, milestone stamps the epic |
| 9 | Stories per layer | Re-slice vertically |
| 10 | Now-lane story has no AC | Move to Next until AC is written |
| 11 | No Won't | Add one |
| 12 | KRs are a to-do list | Rewrite as outcomes; move tasks to Initiatives |
