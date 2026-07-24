---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Roadmap: {{TITLE}}

> Slug: `{{SLUG}}` · Created: `{{CREATED_AT}}` · Owner: `{{OWNER}}`
> Status: <!-- one of: draft / active / complete / cancelled -->

## Brief (verbatim user ask)

{{BRIEF}}

---

## Goal

<!-- Phase 1 — REFINE writes this section. -->

**How might we:** <!-- HMW sentence -->

**Objective:** <!-- one sentence, qualitative, time-bound -->

**Key Results:**
- KR1: <!-- measurable outcome, time-bound, DORA/SLO-shaped where possible -->
- KR2: <!-- measurable outcome, time-bound -->
- KR3: <!-- measurable outcome, time-bound -->

**Assumptions:**
- `[MUST]` <!-- assumption -->. Validation: <!-- how / who -->.
- `[SHOULD]` <!-- assumption -->. Fallback: <!-- plan if false -->.
- `[MIGHT]` <!-- assumption -->. Revisit at Phase 3.

**Won't (explicit non-goals):**
- <!-- non-goal -->
- <!-- non-goal -->

**In-context evidence reviewed:**
- <!-- CLAUDE.md / MCP tool used / file path -->
- <!-- workspace memory entry referenced -->
- <!-- Confluence page if any -->

---

## Epics

<!-- Phase 2 — DECOMPOSE writes this section. -->

**Decomposition technique:** <!-- vertical-slicing / user-story-mapping / impact-mapping / event-storming -->
**Rationale:** <!-- 1-2 lines on why this technique fits the problem shape -->

### E1: <!-- Title — action verb, no conventional commit prefix -->

- **Type:** <!-- enabler | value -->
- **Size:** <!-- XS | S | M | L | XL -->
- **Specialist:** <!-- agent name from data/agents/, or general-purpose -->
- **Depends on:** <!-- other epic id, or none -->
- **Acceptance criteria (epic-level):**
  - <!-- observable outcome -->
  - <!-- observable outcome -->
  - <!-- observable outcome -->
- **Risks:** <!-- 1-2 lines -->

### E2: <!-- Title -->
…

---

## Roadmap — Now / Next / Later

<!-- Phase 3 — SEQUENCE writes this section. -->

### Now (≤ 6 weeks combined for a 1–3 person team)

#### M1: <!-- Title --> — milestone ID `{{SLUG}}-m1`

- [ ] **Status:** pending
- **Source epic:** E1
- **MoSCoW:** Must
- **RICE:** R<!--n--> × I<!--n--> × C<!--n--> ÷ E<!--n--> = <!--score--> (rank #1)
- **Specialist:** <!-- agent name -->
- **Stories:**
  - **S1.1:** <!-- story title --> — Size <!-- XS|S|M -->
    - Given <!-- context --> When <!-- action --> Then <!-- observable outcome -->
    - Given <!-- context --> When <!-- action --> Then <!-- observable outcome -->
    - External writes required: <!-- list -->
  - **S1.2:** <!-- story title --> — Size <!-- XS|S|M -->
    - …
- **Run with:** `/milestone-pipeline {{SLUG}}-m1`

#### M2: <!-- Title --> — milestone ID `{{SLUG}}-m2`
…

### Next (shaped, no story-level AC)

- **E3:** <!-- Title --> — Must, RICE rank #4. Epic-level AC carried from `## Epics` section.
- **E4:** <!-- Title --> — Should. Pull forward when Now lane capacity opens.

### Later (outcomes only)

- **E5:** <!-- Title --> — Should. Outcome: <!-- KR-shaped sentence -->.
- **E6:** <!-- Title --> — Could. Outcome: <!-- KR-shaped sentence -->.

### Spike / discovery lane

- **SP1:** <!-- Validate <assumption>. ≤ 3 days. Output: plans/{{SLUG}}-sp1-decision.md. Blocks M<n>. -->
- <!-- If empty: "All Phase 1 [MUST] assumptions validated from in-context evidence." -->

### Won't (cut from this roadmap horizon)

- <!-- E<n> (Title): cut — reason -->
- <!-- E<n> (Title): cut — reason -->

---

## Definition of Done (universal)

Every story in this roadmap satisfies:

- [ ] Code reviewed by ≥ 1 other engineer (or rectifier sub-agent in `/milestone-pipeline`)
- [ ] Tests pass (project's `make check` or equivalent: `helm template`, `pulumi preview`, `kubectl --dry-run`, `pytest`, `go test`, `npm test`)
- [ ] Conventional commit subject: `^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}`
- [ ] Deployed to dev environment and verified
- [ ] Docs updated (CLAUDE.md, runbooks, README — whatever the change touched)
- [ ] Observability in place (metrics / logs / alerts where applicable)
- [ ] External writes (push, MR, sync, kubectl-apply) explicitly authorized by user

---

## Cross-references

<!-- Phase 4 — MATERIALIZE writes this section. -->

### Milestone execution

- **M1:** `/milestone-pipeline {{SLUG}}-m1`
- **M2:** `/milestone-pipeline {{SLUG}}-m2`

### Spikes

- **SP1:** Decision doc → `plans/{{SLUG}}-sp1-decision.md`
- **SP2:** Decision doc → `plans/{{SLUG}}-sp2-decision.md`

### Related plans

<!-- Other plans/*.md docs (precursor designs, related roadmaps, retrospectives) -->

### GitLab issues

<!-- Populated only if --gitlab was passed and issues were created. Format:
- E1 (epic): #<iid>
  - S1.1: #<iid>
  - S1.2: #<iid>
- E2 (epic): #<iid>
-->

### Confluence pages

<!-- Linked design docs from Platform R&D space -->

### Memory references

<!-- durable references cited during planning: data/references/<name>.md, repo CLAUDE.md sections, or (optional) your own auto-memory topics via search_memory -->

### Review checkpoints

<!-- Optional external-audit tasks — appended by `/handoff review`, inserted via
     handoff-validate.py --insert-checkpoint (see handoff-contract.md §6). Line shape:
     - [ ] (optional) session audit <date> — covers `<slug>-mN`, … · handoff: `plans/<file>` · reviewer: <target> -->

---

## Revision history

- `{{CREATED_AT}}` — Initial roadmap drafted via `/roadmap {{SLUG}}`.
<!-- Append entries as the roadmap is updated:
- 2026-MM-DD — M1 completed; M3 pulled from Next to Now.
- 2026-MM-DD — Re-scoped: removed E5, added E7 per user input.
-->
