---
type: reference
project: spike
status: active
tags:
  - type/reference
  - project/spike
  - status/active
---
# Spike artifacts — typed schemas, provenance, and who writes what

The `/spike` pipeline moved from prose-only artifact contracts (an illustrative
JSON block inside `design.md`) to **typed JSON artifacts validated by
`spike-validate.py`**. Each phase's advance is gated on its artifact validating
and its provenance hashes lining up. This is the artifact-layer authority the
`spike-agent-contract.md` "validate the pointer, read the artifact" rule leans on.

Every artifact is `schema_version: 1`. Validate one with
`spike-validate.py <design|measurements|decision|review> <path>` (exit 0 = valid).

## Provenance model — who computes vs who echoes a hash

The chain that makes a changed upstream artifact invalidate everything downstream:

- **Scripts COMPUTE hashes.** `spike-checkpoint.py` stores each artifact's sha256
  in `state.json` at the transition that accepts it, and re-hashes every upstream
  artifact on every later advance. `spike-decide.py` records the design +
  measurements hashes it derived `decision.json` from.
- **Agents ECHO the hash the orchestrator hands them** (a reliable string copy —
  never ask an LLM to compute a sha256). The executor echoes `design_hash` into
  `measurements.json`; the reviewer echoes `decision_hash` + `note_hash` into
  `review.json`. The orchestrator gets these with `spike-checkpoint.py <id> --get
  <hash-field>` and substitutes them into the dispatch prompt.
- **The checkpoint VERIFIES**: at each advance the echoed hash must equal the
  stored hash, and the on-disk bytes must still hash to the stored value. A
  mismatch is a stale generation or an out-of-band edit → refused.

## design.json — authored by spike-designer

```json
{
  "schema_version": 1,
  "spike_id": "widget-latency-spike-1",
  "assumption": "widget lib X achieves p95 <= 20ms at N=1000",
  "brief_source": "the roadmap bullet text this was extracted from",
  "criteria": [
    {"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20, "unit": "ms"}
  ],
  "sample_size": 1000,
  "sample_justification": "N=1000 gives a stable p95 with <5% variance",
  "confounds": [
    {"confound": "warm-up / JIT", "control": "discard first 100 iterations"},
    {"confound": "GC pauses", "control": "gc.disable() during measurement"},
    {"confound": "I/O buffering", "control": "in-memory only"}
  ],
  "measurement_fields": ["p95_ms"],
  "poc_constraints": {"language": "python3-stdlib", "max_loc": 200, "dependencies": []},
  "cost_estimate_usd": 1.2,
  "authored_at": "2026-07-10T00:00:00Z"
}
```

Rules `spike-validate.py` enforces: ≥1 `criteria`; each criterion has
`name`/`field`/`operator`/`threshold`/`unit`, `field` ∈ `measurement_fields`,
`operator` ∈ `< <= > >= == !=`, and `threshold` numeric for the numeric
operators (number/string/bool for `==`/`!=`); `sample_size` a positive int; ≥3
`confounds`, each with string `confound`+`control`; `measurement_fields`
non-empty. **The criteria are the machine contract** — the executor populates
their fields; the verdict derives from them.

## measurements.json — authored by spike-executor

```json
{
  "schema_version": 1,
  "spike_id": "widget-latency-spike-1",
  "design_hash": "<echoed from `--get design_hash`>",
  "executed_at": "2026-07-10T00:03:00Z",
  "poc_command": "python3 poc/bench.py",
  "poc_hash": "<optional sha256 of the poc/ contents>",
  "iterations": 1,
  "sample_count": 1000,
  "values": {
    "p95_ms": 14.2,
    "some_field": {"unmeasured": true, "reason": "requires a full cluster"}
  }
}
```

`values` carries one entry per `measurement_fields` (the checkpoint refuses
`executed` if a criterion field has no value). A value is a scalar, `null`, or
`{"unmeasured": true, "reason": "…"}`. A `null`/unmeasured value maps to an
UNCERTAIN criterion in the derivation — that is how the executor *declares*
unmeasurability, with a reason, instead of guessing. `design_hash` must echo the
checkpointed one.

## decision.json — DERIVED by spike-decide.py (no agent authors this)

```json
{
  "schema_version": 1,
  "spike_id": "widget-latency-spike-1",
  "design_hash": "…", "measurements_hash": "…",
  "verdict": "YES",
  "per_criterion": [
    {"name": "p95", "field": "p95_ms", "operator": "<=", "threshold": 20,
     "unit": "ms", "measured": 14.2, "result": "pass"}
  ],
  "derived_at": "2026-07-10T00:04:00Z"
}
```

`result` ∈ `pass|fail|unmeasured`. **Verdict rule (deterministic):** any `fail`
→ `NO`; else any `unmeasured` → `UNCERTAIN`; else `YES`. `spike-validate.py`
re-checks that the stored `verdict` equals the verdict its own `per_criterion`
results derive — a hand-tampered `verdict` is refused. This is the structural
fix for the sealed-corpus defect where ACCEPTed spikes carried `""` or
`"GO-ephemeral"`: those tokens are now unrepresentable.

## review.json — authored by spike-reviewer

```json
{
  "schema_version": 1,
  "spike_id": "widget-latency-spike-1",
  "decision_hash": "<echoed from `--get decision_hash`>",
  "note_hash": "<echoed from `--get note_hash`>",
  "reviewer_independent_verdict": "YES",
  "axes": {
    "design_validity": "sound",
    "sample_size": "sound",
    "confound": "sound",
    "methodology": "sound",
    "decision_validity": "sound",
    "implications": "finding: implications hedge on a clear YES"
  },
  "verdict": "RECONSIDER-DECISION",
  "reviewed_at": "2026-07-10T00:06:00Z"
}
```

All six `axes` keys are required, each a non-empty string (`"sound"` or
`"finding: …"`). `verdict` ∈ `ACCEPT|RE-RUN|RECONSIDER-DECISION`. `note_hash`
must echo the checkpointed note hash — this is what catches the classic
RE-RUN-skipped-the-writer bug (a review of a stale note is refused at `reviewed`).

## note.md and review.md — the durable prose

`note.md` (writer) is the human-readable implications record; it **cites** the
derived verdict from `decision.json`, it does not choose one. `review.md`
(reviewer) is the axis-by-axis narrative. Both are gated only on
existence + non-empty (their machine claims live in the JSON siblings), but the
checkpoint hashes `note.md` so the reviewer provenance can bind to it.

## Single-writer table

| Artifact | Sole author | Verified at |
|---|---|---|
| `design.json` + `design.md` | spike-designer | advance `designed` |
| `measurements.json` (+ `poc/`) | spike-executor | advance `executed` |
| `decision.json` | **spike-decide.py** (script, not an agent) | advance `decided` |
| `note.md` | spike-writer | advance `written` |
| `review.json` + `review.md` | spike-reviewer | advance `reviewed` |
| `design-deviation.md` | spike-executor (flawed-design path) | `--deviation` loop |
