---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Spike Protocol — Single Source of Truth

A spike is a **time-boxed discovery run** that answers exactly **one** assumption with a YES / NO / UNCERTAIN verdict backed by measured data. It is not a milestone, a refactor, a tool integration, or exploratory coding. POC code is throwaway.

The verdict is **derived deterministically** from typed success criteria and measured values (`spike-decide.py`), not authored by an agent. Run state lives in a machine `state.json` phase machine (`spike-checkpoint.py`), not inferred from which files exist. See `spike-state-schema.md` + `spike-artifact-schema.md`.

---

## What a Spike IS vs IS NOT

| IS | IS NOT |
|---|---|
| Tests one specific assumption | Tests multiple assumptions at once |
| Produces a verdict DERIVED from measurements.json | Produces "interesting findings" without a verdict |
| POC code is sandboxed, throwaway | POC code merged into main-tree |
| Time-boxed (soft $1–2, hard $5) | Open-ended exploration |
| Answers a binary or three-way question | Proves a design is good in general |
| Precedes a milestone to reduce risk | Replaces a milestone |
| Durable artifact is note.md + review.md | Durable artifact is the POC code |

---

## The 4 Phases (+ the deterministic decide step)

| Phase | Agent / script | Model | Reads | Writes |
|---|---|---|---|---|
| 1. Design | spike-designer | `deep-reasoning-high` | roadmap brief | design.json + design.md |
| 2. Execute | spike-executor | `balanced-high` (`deep-reasoning-high` w/ `--executor deep`) | design.json | poc/*, measurements.json OR design-deviation.md |
| — Decide | **spike-decide.py** (script) | — | design.json + measurements.json | decision.json (the DERIVED verdict) |
| 3. Write-up | spike-writer | `fast-mechanical` | design.json + measurements.json + decision.json | note.md (cites the verdict) |
| 4. Review | spike-reviewer | `deep-reasoning-max` | all artifacts | review.json + review.md |

The main session is the orchestrator; it drives `spike-checkpoint.py` between phases. Sub-agents never spawn other sub-agents. The writer no longer chooses the verdict — it explains the implications of the verdict the decide step already computed.

---

## Decision Categories (derived, not authored)

| Verdict | Derivation rule | Example |
|---|---|---|
| YES | every criterion `pass` | "p95 ≤ 20 ms at N=1000; measured 14 ms" |
| NO | any criterion `fail` | "measured 380 ms; threshold 20 ms; not bridgeable without redesign" |
| UNCERTAIN | no `fail`, ≥1 `unmeasured` | "cannot be measured in the target Istio mesh without a full cluster" |

`spike-decide.py` computes this from `design.json` criteria + `measurements.json` values. UNCERTAIN is declared by the executor writing a `{"unmeasured": true, "reason": …}` value — a precise methodology gap, never "results suggest maybe". The three tokens are the ONLY possible verdicts; the sealed-corpus defect (an ACCEPTed spike with `""` or `"GO-ephemeral"`) is now unrepresentable.

---

## Review Verdicts

| Verdict | Axes failing | Next step |
|---|---|---|
| ACCEPT | None | Orchestrator advances to `complete` (gated on ACCEPT + a canonical verdict); note.md is the durable record |
| RE-RUN | Design validity, sample-size, confound, or methodology execution (axes 1–4) | `--rerun`: new attempt → designer → executor → **decide → writer** → reviewer (the note IS regenerated) |
| RECONSIDER-DECISION | Axes 1–4 sound; the note's *implications* are wrong (axis 5–6) | `--reconsider`: re-dispatch writer only (same measurements + derived verdict, new note) |

Note: because the verdict is derived, RECONSIDER-DECISION is about the note's **implications/narrative**, not the verdict token. If the reviewer believes the verdict itself is wrong, that is a claim about the criteria or measurements → RE-RUN.

---

## Loop Caps (survive restart — stored in state.json)

| Loop | Max | Action on cap |
|---|---|---|
| RE-RUN (designer + executor) | 2 | `--terminal rerun-cap`; surface to user |
| RECONSIDER-DECISION (writer) | 2 | `--terminal reconsider-cap`; surface to user |
| design-deviation (re-design) | 2 | `--terminal design-deviation-cap`; surface to user |

`spike-checkpoint.py` refuses the loop op once the counter hits the cap — the count lives in `state.json`, so it is honored even across a killed-and-resumed session.

---

## Cost Discipline

- **Soft cap:** ~$1–$2 per spike (entire run). **Hard cap:** $5.
- Executor: stdlib-only Python by default; no `pip install` during execution. Justify any dependency in design.json before executor starts.
- If the soft cap is exhausted and the spike is unresolved, surface to user before continuing.

---

## Sandboxing Rules

- All POC code lives under `.claude/notes/spikes/<id>/poc/` — never in the main source tree.
- Default POC: stdlib-only Python 3, ≤ 200 LOC, runnable as `python3 poc/<script>.py`.
- Executor must not write outside the spike sandbox. If the design requires it, return `status: design-deviation` instead of violating the sandbox.

---

## When a Spike Result Invalidates a Downstream Milestone

- Orchestrator surfaces the implications section of note.md to the user.
- **Orchestrator NEVER auto-mutates roadmap files.** A NO verdict does not give the pipeline permission to edit `plans/*.md`. The user owns the roadmap.
- **Spike-as-prerequisite (dependency gate).** A downstream milestone can require this spike by listing its id in the milestone's `depends_on` (roadmap milestones register). The milestone dependency gate then refuses to start that milestone until this spike reaches the **ACCEPT terminal** — a `--skip-review` / capped / aborted spike does NOT unblock it (override-able + audited). The gate reads this spike's local `state.json` read-only; the spike pipeline never writes the register. See `roadmap-milestones-schema.md` (Dependency gate).

---

## File Tree

```
.claude/notes/spikes/<id>/
├── state.json             # machine state (phase machine) — spike-checkpoint.py owns it
├── design.json            # Phase 1 typed contract (designer)
├── design.md              # Phase 1 human design (designer)
├── poc/                   # Phase 2 sandbox (executor) — ≤200 LOC, stdlib-only, throwaway
├── measurements.json      # Phase 2 measured values (executor)
├── design-deviation.md    # Phase 2 (executor, if design is flawed)
├── decision.json          # DERIVED verdict (spike-decide.py) — not authored
├── note.md                # Phase 3 implications (writer) — cites the verdict
├── review.json            # Phase 4 typed review (reviewer)
└── review.md              # Phase 4 review narrative (reviewer)
```

Lock file (gitignored): `.claude/notes/spikes/.lock` — a single coarse orchestrator lock (one spike at a time, system-wide). Acquired atomically (O_EXCL) by `spike-init.sh`; there is **no** PID-liveness auto-clear (the pre-v2 lock stored a dead PID and never actually gated). A crashed run holds the lock until an explicit `spike-release.sh <id>` (or `--force`).

---

## Artifact Commitment Policy

**Spike artifacts are LOCAL-ONLY — gitignored, NOT committed** (all of `state.json`, the JSON artifacts, `poc/`, `note.md`, `review.md`). CI Gate 1d fails the pipeline if a spike `state.json` under `.claude/notes/spikes/` is ever tracked. To share a conclusion, promote it into the roadmap (`plans/<slug>-roadmap.md`) or a memory tier via `/memory-sync` — those ARE committed. The workspace CLAUDE.md `/spike` cadence section wins if this file ever disagrees.

Terminal states (each recorded by `spike-checkpoint.py`; the emit fires exactly once, then `spike-release.sh` drops the lock):
- Advance to `complete` — reviewer `ACCEPT` (+ canonical verdict). The ACCEPT terminal.
- `--terminal skip-review` — `--skip-review`, writer done (requires a canonical derived verdict).
- `--terminal rerun-cap` / `reconsider-cap` / `design-deviation-cap` — a loop cap reached.
- `--terminal brief-inadequate` — designer could not extract/answer the brief.
- `--terminal aborted-scope` — any agent returned aborted-scope.
- `--terminal reviewer-malformed` — reviewer returned but review.json/verdict was malformed.
