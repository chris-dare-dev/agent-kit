---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Frameworks reference

Lean default loaded by every phase. Long-tail loaded only when a non-default fits.

## Lean default (always in scope)

| Concern | Default framework | Used in phase |
|---|---|---|
| Goal-setting | **OKR** (Objective + 2–4 outcome KRs) | 1 |
| Story-level commitment | **SMART** (within story AC) | 3 |
| Decomposition (default) | **Vertical slicing + enabler stories** | 2 |
| Story sizing | **t-shirt** (XS / S / M / L / XL); cap stories at M (≤3 days) | 3 |
| Prioritization (cut-line) | **MoSCoW** (Musts capped ≤ 60%) | 3 |
| Prioritization (ranking Musts) | **RICE** (R × I × C ÷ E) | 3 |
| Roadmap shape | **Now / Next / Later** (rolling-wave) | 3 |
| Acceptance criteria (story) | **Given / When / Then** (Gherkin) | 3 |
| Definition of Done | Universal bullet checklist (lives once) | 3 |
| Discovery | **Spike lane** (≤ 3 days, output: decision doc) | 3 |

These are the rules baked into the phase-*.md references. The orchestrator does NOT need to read this file unless a non-default situation arises.

## Long-tail (read this file only when needed)

### Goal-setting alternatives

| Framework | When to reach for it | Citation |
|---|---|---|
| **SMART goals** (Specific/Measurable/Achievable/Relevant/Time-bound) | Single-quarter individual commitments where 100% is the bar (vs OKR's 70%) | Doran, "There's a S.M.A.R.T. way to write management's goals", *Management Review* 1981 |
| **HOTS** (Hierarchy of Outcomes / Tools / Steps) | When the team owns deeply technical execution and outcome decomposition matters | Bain consulting framework |
| **DORA metrics** as KRs | Default scaffold for engineering KRs (lead time, deploy freq, change-failure rate, MTTR) | Forsgren/Humble/Kim, *Accelerate*, 2018 |

### Decomposition alternatives

| Technique | When to reach for it | Output | Citation |
|---|---|---|---|
| **User Story Mapping** | User-facing feature with a clear journey | Backbone of activities → walking-skeleton release slices | Patton, *User Story Mapping*, O'Reilly 2014 |
| **Event Storming** | Domain you don't yet understand (new tenant model, new auth flow) | Domain events → bounded contexts → first epics | Brandolini, *Introducing EventStorming*, Leanpub |
| **Impact Mapping** | Behavior-change goal with unclear scope ("make X faster", "improve adoption") | Goal → Actors → Impacts → Deliverables | Adzic, *Impact Mapping*, 2012 |
| **SPIDR** (Spike / Path / Interface / Data / Rules) | Splitting a story that's too big | 2–5 smaller vertical slices | Cohn, "Patterns for Splitting User Stories" |

### Prioritization alternatives

| Framework | Formula / shape | When | Failure mode |
|---|---|---|---|
| **WSJF** (Weighted Shortest Job First, SAFe) | Cost-of-Delay ÷ Job size | Multiple teams competing for capacity, economic framing | Hard to estimate Cost-of-Delay; often gamed |
| **ICE** | Impact × Confidence × Ease (1–10) | Early-stage spikes / experiments, low data | Subjectivity; one person inflates scores |
| **Kano** | Basic / Performance / Attractive / Indifferent | UX/feature mix; balancing table-stakes vs delight | Expensive (needs user survey) |
| **Cost of Delay alone** | $ lost per week of delay | Time-sensitive single decisions (release window, compliance) | Estimating $ in infra contexts |
| **Stack ranking (forced order)** | Linear list of priorities | Small backlog (≤ 10 items), single decision-maker | Doesn't scale; loses nuance |

### Roadmap-shape alternatives

| Format | When | Source |
|---|---|---|
| **GIST** (Goals / Ideas / Step-projects / Tasks) | When goals & ideas need to be tracked separately from execution | Itamar Gilad, 2017 |
| **Outcome-based** | Pair with Now/Next/Later — each lane carries an outcome metric | Teresa Torres, *Continuous Discovery Habits* |
| **Theme-based** | Annual planning, one tier above Now/Next/Later | — |
| **Gantt** | Avoid for software; only for hard external dependencies (vendor, regulatory date) | — |

### Process model alternatives

| Model | Fit |
|---|---|
| **Scrum** | Heavy ceremonies, sprint commitments — overkill for a small platform team |
| **Kanban** | Default for ops-heavy work; flow + WIP limits + cycle time |
| **Shape Up** (Basecamp) | Best fit when team owns its roadmap: 6-week shaped cycles + 2-week cooldown, fixed time / variable scope, no backlog grooming theatre. Ryan Singer, *Shape Up*, Basecamp 2019 |
| **Scrumban** | Pragmatic mix — cadence from Scrum, flow from Kanban |

For workspace's small platform team in 2026: Shape Up for project-shaped work (a migration, a new capability), Kanban for the always-on operational stream. This skill's Now/Next/Later format is compatible with either — Now ≈ current Shape Up cycle / current Kanban WIP.

### Estimation alternatives

| Style | When | Skip when |
|---|---|---|
| Story points (Fibonacci) | Mixed-experience team, stakeholders demand velocity | Senior team with stable throughput |
| Ideal days | Single-owner tasks, deadline math | Anything with collaboration overhead |
| **#NoEstimates** (count items, forecast by throughput) | Small senior team with right-sized stories | Team still learning to slice |

Modern (post-2023) consensus for senior platform teams: right-size every story to ≤ 3 days and forecast by throughput (count). Don't go #NoEstimates without first practicing sizing — it's a graduation, not a shortcut.

## Decision rules (when does each apply?)

- **OKR vs SMART:** If the team controls *how* but not *whether* the outcome happens → KR. If they control both → SMART. (Engineering: SLO improvements are KRs; "ship migration X by date Y" is SMART.)
- **MoSCoW vs RICE:** MoSCoW is for the cut-line decision (yes / no / later); RICE is for ordering the yeses. Don't substitute one for the other.
- **Vertical-slicing vs Story Mapping:** Vertical for platform (no user journey to map); Story Mapping for product features.
- **Now/Next/Later vs Gantt:** Now/Next/Later when scope is variable and you're being honest about uncertainty; Gantt only when you have a hard external date (vendor cutover, compliance deadline) AND scope is locked.
- **Spike vs epic:** Spike output is a decision doc; epic output is shipped value. If the team can't size the work without a spike, do the spike first.
- **Shape Up vs Kanban:** Shape Up for project-shaped work with appetite + scope tradeoffs; Kanban for steady ops flow. Most platform teams run both lanes.

## Authoritative references

- John Doerr, *Measure What Matters*, 2018 — https://www.whatmatters.com/
- Andy Grove, *High Output Management*, 1983 (origin of OKRs)
- Doran, "S.M.A.R.T.", *Management Review* 1981
- Atlassian, "Epics, Stories, Themes, and Initiatives" — https://www.atlassian.com/agile/project-management/epics-stories-themes
- Jeff Patton, *User Story Mapping*, O'Reilly 2014 — https://www.jpattonassociates.com/user-story-mapping/
- Alberto Brandolini, *Introducing EventStorming*, Leanpub — https://www.eventstorming.com/
- Gojko Adzic, *Impact Mapping*, 2012 — https://www.impactmapping.org/
- Bill Wake, "INVEST in Good Stories, and SMART Tasks", 2003 — https://xp123.com/articles/invest-in-good-stories-and-smart-tasks/
- Sean McBride / Intercom, "RICE" — https://www.intercom.com/blog/rice-simple-prioritization-for-product-managers/
- Dean Leffingwell / SAFe, WSJF — https://framework.scaledagile.com/wsjf
- Noriaki Kano, "Attractive quality", 1984
- Donald Reinertsen, *The Principles of Product Development Flow*, 2009 (Cost of Delay)
- Janna Bastow, "Now/Next/Later roadmap", 2012 — https://www.prodpad.com/blog/the-now-next-later-roadmap/
- Itamar Gilad, "GIST Planning", 2017 — https://itamargilad.com/gist/
- Teresa Torres, *Continuous Discovery Habits*, 2021 — https://www.producttalk.org/
- Ryan Singer, *Shape Up*, Basecamp 2019 — https://basecamp.com/shapeup
- Dan North, "Introducing BDD" (Given/When/Then) — https://dannorth.net/introducing-bdd/
- Forsgren/Humble/Kim, *Accelerate*, 2018 (DORA metrics)
- Vasco Duarte, *NoEstimates*, 2015 — https://noestimatesbook.com/
