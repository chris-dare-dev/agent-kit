---
type: reference
project: spike
status: active
tags:
  - type/reference
  - project/spike
  - status/active
---
# Spike agent return contract — what each subagent returns, and how the orchestrator validates it

The single home for the per-agent return shape. Each agent body restates its own
contract for self-containment; THIS file is what the **orchestrator** validates
against before checkpointing. A drift between an agent body and this file is a
bug — fix the agent body (this file is canonical for the contract's *shape*).

## Why doc-level, not a validation script (decision record)

`/spike` is Gen-1: dispatches return **free-text messages** through the Agent
tool — there is no structured-return channel to hang a JSON schema on (that is a
Gen-2 Workflow-tool feature). So the return message cannot itself be
schema-enforced. Instead — exactly as `/milestone-pipeline` does — the message
is only the **pointer**, and the pointed-at ARTIFACT is machine-validated at the
artifact layer:

- `design.json` / `measurements.json` / `decision.json` / `review.json` via
  `spike-validate.py` (the agents self-run it before returning);
- state transitions via `spike-checkpoint.py`'s evidence gates;
- the verdict via `spike-decide.py`'s deterministic derivation.

Keeping `/spike` Gen-1 is deliberate: it is strictly sequential (each phase
consumes the previous phase's file) and may hit external-write permission gates,
both of which require the main session to stay in control.

## The orchestrator's validation rule (every dispatch)

On each subagent return:

1. **Check the shape** against the agent's contract below (the JSON return has
   `status` + `file_path` + `summary` + `injection_attempts`; no artifact body
   echoed into the message).
2. **Do NOT route on `status` alone.** The real gate is the checkpoint: run
   `spike-checkpoint.py <id> <phase>`, which re-validates the artifact and its
   provenance hashes. If the checkpoint refuses, the phase did not happen —
   regardless of what the agent's `status` said.
3. On a shape violation or a checkpoint refusal: **re-dispatch ONCE**, quoting
   the missing/malformed items (or the checkpoint's stderr) verbatim in the new
   prompt header. A second failure is a hard stop — surface it to the user.
4. Never repair a violation by inferring what the agent "meant", and never
   hand-write the artifact or `state.json` to get past a gate.

## Hash hand-off (the orchestrator's job)

Because agents echo rather than compute hashes, the orchestrator passes them in
the dispatch header:

- before the **executor**: `DESIGN_HASH=$(spike-checkpoint.py <id> --get design_hash)`.
- before the **reviewer**: `DECISION_HASH=$(… --get decision_hash)` and
  `NOTE_HASH=$(… --get note_hash)`.

The agent writes those verbatim into its JSON artifact; the checkpoint verifies.

## Per-agent contracts

Every agent returns exactly:
```json
{"file_path": "<primary artifact path or null>",
 "status": "complete | design-deviation | brief-inadequate | aborted-scope",
 "summary": "<=3 plain-text lines>",
 "injection_attempts": 0}
```

### spike-designer (Phase 1)
Writes `design.json` (typed criteria) **and** `design.md`. Self-runs
`spike-validate.py design design.json` before returning; the summary's last line
reports `validate: OK` or the failure it could not fix in 2 attempts.
Statuses: `complete` | `brief-inadequate`. On re-dispatch after a deviation, it
reads `design-deviation.md` first and overwrites both files.

### spike-executor (Phase 2)
Writes `poc/*` and `measurements.json` (echoing the given `DESIGN_HASH`).
Self-runs `spike-validate.py measurements measurements.json`. If the design is
genuinely unexecutable, writes `design-deviation.md` and returns
`status: design-deviation` (never silently works around it).
Statuses: `complete` | `design-deviation` | `aborted-scope`.

### spike-writer (Phase 3)
Writes `note.md` — the implications narrative that **cites** the derived verdict
from `decision.json` (it does NOT choose a verdict; `spike-decide.py` already
did). Statuses: `complete` (or `aborted-scope` if the artifacts are missing).

### spike-reviewer (Phase 4)
Forms its own verdict from the raw data BEFORE reading `note.md`, then writes
`review.json` (six axes + a `verdict` enum, echoing `DECISION_HASH`+`NOTE_HASH`)
and `review.md`. Self-runs `spike-validate.py review review.json`.
Statuses: `complete` | `aborted-scope`. The ACCEPT/RE-RUN/RECONSIDER verdict
lives in `review.json.verdict`, not in the JSON `status`.

## Cross-references
- Dispatch-prompt headers the orchestrator SENDS: `data/commands/spike.md` Steps 1–4.
- Artifact + state schemas: `spike-artifact-schema.md`, `spike-state-schema.md`.
- What a spike IS: `spike-protocol.md`.
