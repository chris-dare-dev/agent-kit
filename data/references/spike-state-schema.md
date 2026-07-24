---
type: reference
project: spike
status: active
tags:
  - type/reference
  - project/spike
  - status/active
---
# Spike `state.json` — schema and single-writer rules

`state.json` is the **machine authority** for a `/spike` run: phase, attempt,
loop budgets, artifact provenance hashes, the derived verdict, the reviewer
verdict, terminal status, and exactly-once outcome emission. The human-readable
artifacts (`design.md`, `note.md`, `review.md`) are *projections*; this file is
what the orchestrator and the gates trust.

One writer owns it: **`spike-checkpoint.py`**. No agent, and no human, hand-edits
`state.json`. If you think you need to, you have found a missing checkpoint
operation — add it there, do not bypass the gate.

## Location & commitment

`<repo-root>/.claude/notes/spikes/<spike-id>/state.json` — **LOCAL-ONLY,
gitignored, never committed** (same policy as the roadmap register and the
milestone `state.json`; `ensure-claude-gitignore.sh` excludes the whole
`.claude/notes/` tier). CI Gate 1d fails the pipeline if a `state.json` under
`.claude/notes/spikes/` is ever tracked.

## Shape (schema_version 1)

```json
{
  "schema_version": 1,
  "spike_id": "widget-latency-spike-1",
  "created_at": "2026-07-10T00:00:00Z",
  "updated_at": "2026-07-10T00:05:00Z",
  "phase": "reviewed",
  "attempt": 2,
  "rerun_count": 1,
  "reconsider_count": 0,
  "deviation_count": 0,
  "design_hash": "…64 hex…",
  "measurements_hash": "…64 hex…",
  "decision_hash": "…64 hex…",
  "note_hash": "…64 hex…",
  "review_hash": "…64 hex…",
  "verdict": "YES",
  "review_verdict": "ACCEPT",
  "skipped_review": false,
  "terminal_status": null,
  "outcome_emitted": false,
  "roadmap_path": "plans/widgets-roadmap.md",
  "brief_source": "…bullet text…",
  "phase_history": [{"phase": "init", "at": "…"}, {"phase": "designed", "at": "…"}],
  "attempt_history": [{"kind": "rerun", "attempt": 2, "at": "…"}]
}
```

## Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int (==1) | Any other value is refused, never guessed. |
| `spike_id` | str | `^[a-z][a-z0-9-]*-spike-[0-9]+$`; a path segment, so no separators. |
| `phase` | enum | `init · designed · executed · decided · written · reviewed · complete`. |
| `attempt` | int | Bumped by `--rerun` / `--deviation` (a fresh design→measure pass). |
| `rerun_count` / `reconsider_count` / `deviation_count` | int | Loop budgets — **survive restart** (they live here, not in the session). Cap 2 each. |
| `design_hash` … `review_hash` | str\|null | sha256 of each artifact as of the transition that recorded it. The provenance chain. |
| `verdict` | enum\|null | The **derived** spike answer `YES/NO/UNCERTAIN`, mirrored from `decision.json`. Never authored by an agent. |
| `review_verdict` | enum\|null | `ACCEPT/RE-RUN/RECONSIDER-DECISION`, mirrored from `review.json`. |
| `skipped_review` | bool | True only on the audited `--skip-review` terminal. |
| `terminal_status` | enum\|null | Set once at the terminal (see below). Non-null ⇒ no further advance. |
| `outcome_emitted` | bool | Exactly-once guard for the pipeline-outcome record. |
| `roadmap_path` / `brief_source` | str\|null | Provenance of the brief. |
| `phase_history` / `attempt_history` | list | Append-only audit trails. |

## Phase machine

Forward-only **within an attempt**; one step at a time. `spike-checkpoint.py`
refuses backward and skipped transitions. Loops reset the phase explicitly:

```
init → designed → executed → decided → written → reviewed → complete
                                   ▲                              (ACCEPT terminal)
      --rerun  ─────────────────── resets phase→init, attempt+1, clears all hashes
      --deviation ──────────────── resets phase→init, attempt+1, clears all hashes
      --reconsider ─────────────── resets phase→decided, drops note+review only
```

Each forward step is **evidence-gated** — the target phase cannot be entered
until its artifact exists, VALIDATES (`spike-validate.py`), and its provenance
hashes line up (see `spike-artifact-schema.md`). The gate re-hashes every
upstream artifact on every advance, so an out-of-band edit to `design.json`
after `designed` makes `executed` refuse (stale generation caught, not resumed).

## Terminal statuses

`complete` is the ACCEPT terminal (reached by advancing; it requires
`review_verdict==ACCEPT` **and** a canonical `verdict`). Every other terminal is
recorded with `--terminal <status>`:

`skip-review` (requires a canonical verdict — the writer ran) · `rerun-cap` ·
`reconsider-cap` · `design-deviation-cap` · `brief-inadequate` · `aborted-scope`
· `reviewer-malformed` · `unexpected`.

Setting a terminal emits **exactly one** pipeline-outcome record (guarded by
`outcome_emitted`). `spike-release.sh` no longer emits — it only drops the lock.

## Single-writer table

| Fields | Sole writer | When |
|---|---|---|
| `schema_version`, `spike_id`, `created_at`, skeleton | `spike-checkpoint.py --init` | Once, at init (idempotent). |
| `phase`, `phase_history`, all `*_hash`, `verdict`, `review_verdict` | `spike-checkpoint.py <phase>` (evidence-gated advance) | Per forward step. |
| `attempt`, `*_count`, `attempt_history` | `spike-checkpoint.py --rerun/--reconsider/--deviation` | Per loop. |
| `terminal_status`, `skipped_review`, `outcome_emitted` | `spike-checkpoint.py --terminal` / advance to `complete` | Once, at the terminal. |
| `roadmap_path`, `brief_source` | `spike-checkpoint.py --set` (the ONLY hand-settable fields) | As the orchestrator learns them. |

## Versioning

`schema_version` is frozen at 1. An additive nullable field is a no-bump change
(readers use `dict.get`). A renamed/removed/retyped field bumps the version and
`spike-checkpoint._load` refuses to mutate any other version. See also
`spike-artifact-schema.md` (the artifacts) and `spike-agent-contract.md` (what
each agent returns).
