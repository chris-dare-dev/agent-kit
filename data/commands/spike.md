---
description: Run a 4-phase spike pipeline (Design → Execute → Write-up → Review) that answers exactly one assumption with a YES / NO / UNCERTAIN verdict DERIVED from measured data. State is a machine phase-machine (spike-checkpoint.py), artifacts are typed + validated (spike-validate.py), and the verdict is computed, not authored (spike-decide.py). Use when the user invokes `/spike <id>`, says "run a spike on …", or has a spike entry in their roadmap. The spike must be defined in plans/<slug>-roadmap.md first — use /roadmap to add it if it does not exist yet.
argument-hint: "<spike-id> [--resume] [--executor deep] [--skip-review]"
type: command
status: active
tags:
  - type/command
  - status/active

---

# Spike Pipeline Orchestrator

A spike answers exactly **one** assumption with measured data. POC code is throwaway; the durable artifacts are `note.md` and `review.md`. In spike v2 the run is governed by a **machine state file** (`state.json`, owned by `spike-checkpoint.py`) — every phase transition is evidence-gated, the verdict is **derived** (`spike-decide.py`), and completion is never inferred from which files happen to exist. Read `data/references/spike-protocol.md`, `spike-state-schema.md`, `spike-artifact-schema.md`, and `spike-agent-contract.md`.

**Arguments:** `$ARGUMENTS` — parse as `<spike-id> [--resume] [--executor deep] [--skip-review]`

- `<spike-id>` — required. Everything before the first `--`. **Shape:** `^[a-z][a-z0-9-]*-spike-[0-9]+$` (validated in Step 0). The id is `<topic>-spike-<N>`.
- `--resume` — read `state.json`; continue from the phase it records. (Resume is state-driven regardless of this flag; it just skips the "fresh vs resume" prompt.)
- `--executor deep` — run the executor at policy class `deep-reasoning-high` (dispatch model override — pass only the model token: <!-- {{model-policy:deep-reasoning-high}} -->fable (effort: high)<!-- /model-policy -->) instead of its default class `balanced-high`. Roughly doubles cost. Legacy spelling `--executor opus` is accepted. <!-- model-policy:ok -->
- `--skip-review` — stop after Phase 3 (note.md); record the audited `skip-review` terminal (which still requires a canonical derived verdict). Use only for quick internal spikes.

If `$ARGUMENTS` is empty or has no spike-id, STOP and ask: "What is the spike id? (e.g., `kiali-multicluster-spike-1`)".

---

## When to use / When NOT to use

| Use `/spike` | Use `/milestone-pipeline` | Use neither |
|---|---|---|
| Answering ONE binary/three-way assumption | Shipping code that implements a feature | Exploration without a specific question |
| Validating a design decision before committing | Full research → implement → critique → rectify | Refactors, lint, doc-only changes |
| Time-boxed, throwaway POC | Durable, reviewed, committed code | Single-file fixes (use Edit directly) |

**Key distinction:** a milestone ships code; a spike answers a question. If you want to merge the POC, stop the spike and open a milestone.

---

## Inputs

The spike brief comes from a roadmap file (bullet pattern first; the designer searches H2/H3 headings only after the bullet pattern fails):

```markdown
### Spike / discovery lane
- **`kiali-multicluster-spike-1`** — Does library X achieve p95 ≤ 20 ms at N=1000 in our Istio mesh?
```

Invoke with the roadmap present in the CWD repo's `plans/`:
```
/spike kiali-multicluster-spike-1
/spike token-controller-spike-3 --resume
/spike admin-mcp-spike-2 --executor deep
/spike thanos-federation-spike-1 --skip-review
```

---

## Phase summary

| Phase | Agent / script | Model class | Writes | Advances to |
|---|---|---|---|---|
| 1. Design | spike-designer | `deep-reasoning-high` | design.json + design.md | `designed` |
| 2. Execute | spike-executor | `balanced-high` (`deep-reasoning-high` w/ `--executor deep`) | poc/*, measurements.json OR design-deviation.md | `executed` |
| — Decide | **spike-decide.py** | — | decision.json (DERIVED verdict) | `decided` |
| 3. Write-up | spike-writer | `fast-mechanical` | note.md | `written` |
| 4. Review | spike-reviewer | `deep-reasoning-max` | review.json + review.md | `reviewed` → `complete` on ACCEPT |

---

## Runtime contract (CWD + script invocation)

- **Required CWD: inside the repo whose `plans/<slug>-roadmap.md` defines the spike** — the roadmap lookup and the relative `SPIKE_DIR` paths resolve from CWD.
- `.sh` scripts run `bash "$WS/.claude/scripts/<f>.sh"`; `.py` scripts run `python3 "$WS/.claude/scripts/<f>.py"`.

```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
[ -d "$WS" ] || { echo "Set PERSONAL_WORKSPACE_ROOT to your the workspace" >&2; exit 1; }
CKPT() { python3 "$WS/.claude/scripts/spike-checkpoint.py" "$@"; }
```

---

## Orchestrator steps

### Step 0 — Validate, initialize, and resume from state

```bash
bash "$WS/.claude/scripts/validate-spike-id.sh" "$SPIKE_ID"   # shape check; STOP + report on fail
```

Find the roadmap (prefer the one mentioning the id):
```bash
ROADMAP_PATH=""
if [[ -d plans ]]; then
  ROADMAP_PATH=$(grep -l "$SPIKE_ID" plans/*-roadmap.md 2>/dev/null | head -1)
  [[ -z "$ROADMAP_PATH" ]] && ROADMAP_PATH=$(find plans/ -name "*-roadmap.md" 2>/dev/null | head -1)
fi
```
`ROADMAP_PATH` may be empty on a late-phase resume — that is fine; only the designer (Step 1) needs it, so the requirement is deferred to there (below), NOT enforced here. Do not bail yet.

Initialize (acquires the coarse lock atomically + creates state.json). If the lock is held by a different spike, `spike-init.sh` exits non-zero with the recovery command — surface it and STOP:
```bash
bash "$WS/.claude/scripts/spike-init.sh" "$SPIKE_ID" --repo-root "$(git rev-parse --show-toplevel)" ${ROADMAP_PATH:+--roadmap-path "$ROADMAP_PATH"}
```

Set the artifact paths:
```bash
SPIKE_DIR=".claude/notes/spikes/$SPIKE_ID"
DESIGN_JSON_PATH="$SPIKE_DIR/design.json";  DESIGN_PATH="$SPIKE_DIR/design.md"
MEASUREMENTS_PATH="$SPIKE_DIR/measurements.json"; DECISION_PATH="$SPIKE_DIR/decision.json"
NOTE_PATH="$SPIKE_DIR/note.md"
REVIEW_JSON_PATH="$SPIKE_DIR/review.json"; REVIEW_PATH="$SPIKE_DIR/review.md"
DEVIATION_PATH="$SPIKE_DIR/design-deviation.md"
```

**Resume from the recorded phase** (state-driven — NOT file presence):
```bash
PHASE=$(CKPT "$SPIKE_ID" --get phase)
TERMINAL=$(CKPT "$SPIKE_ID" --get terminal_status)
```

| PHASE (and TERMINAL) | Jump to |
|---|---|
| TERMINAL set | Already finished (`$TERMINAL`). Report + release lock if held. STOP. |
| `init` | Step 1 (designer) |
| `designed` | Step 2 (executor) |
| `executed` | Step 2b (decide) then Step 3 |
| `decided` | Step 3 (writer) |
| `written` | Step 4 (reviewer) |
| `reviewed` | Step 4 routing (read `review_verdict`, ACCEPT→complete else loop) |
| `complete` | Done — release lock if held. STOP. |

**The checkpoint advance is the real gate.** After every agent returns, run the advance for that phase; if it exits non-zero, the phase did NOT happen — re-dispatch the agent ONCE with the checkpoint's stderr quoted in the header (per `spike-agent-contract.md`). A second failure is a hard stop.

---

### Step 1 — Dispatch spike-designer

**Roadmap required here (deferred from Step 0).** The designer needs a brief. If `ROADMAP_PATH` is empty (no `plans/*-roadmap.md`), do NOT dispatch: `CKPT "$SPIKE_ID" --terminal aborted-scope`, `bash "$WS/.claude/scripts/spike-release.sh" "$SPIKE_ID"`, tell the user to run `/roadmap <topic>` first (add the spike entry), and STOP. (A late-phase resume never reaches here, so a moved roadmap does not block finishing an in-flight spike.)

Dispatch ONE spike-designer. Header:
```
SPIKE_ID={SPIKE_ID}
ROADMAP_PATH={ROADMAP_PATH}
DESIGN_JSON_PATH={DESIGN_JSON_PATH}
DESIGN_PATH={DESIGN_PATH}
DEVIATION_PATH={DEVIATION_PATH}   # empty unless re-dispatched after a deviation
VALIDATE_SCRIPT={WS}/.claude/scripts/spike-validate.py   # substitute the resolved absolute $WS path
```

On return, route on `status`, then GATE:

| status | action |
|---|---|
| `complete` | `CKPT "$SPIKE_ID" designed` — if it refuses, re-dispatch once with the stderr; else proceed to Step 2 |
| `brief-inadequate` | `CKPT "$SPIKE_ID" --terminal brief-inadequate`; `bash "$WS/.claude/scripts/spike-release.sh" "$SPIKE_ID"`; surface the gap; STOP |
| `aborted-scope` | `CKPT "$SPIKE_ID" --terminal aborted-scope`; release; STOP |

---

### Step 2 — Dispatch spike-executor

Hand off the design hash (the executor echoes it — never recomputes):
```bash
DESIGN_HASH=$(CKPT "$SPIKE_ID" --get design_hash)
```

If `--executor deep` (or legacy `--executor opus`) <!-- model-policy:ok -->, dispatch with the model from policy class `deep-reasoning-high` (<!-- {{model-policy:deep-reasoning-high}} -->fable (effort: high)<!-- /model-policy -->) as the orchestrator's model hint. Otherwise dispatch with no override.

Header:
```
SPIKE_ID={SPIKE_ID}
DESIGN_JSON_PATH={DESIGN_JSON_PATH}
DESIGN_PATH={DESIGN_PATH}
DESIGN_HASH={DESIGN_HASH}
POC_DIR={SPIKE_DIR}/poc
MEASUREMENTS_PATH={MEASUREMENTS_PATH}
VALIDATE_SCRIPT={WS}/.claude/scripts/spike-validate.py   # substitute the resolved absolute $WS path
```

On return:

| status | action |
|---|---|
| `complete` | `CKPT "$SPIKE_ID" executed` (re-dispatch once on refusal) → Step 2b |
| `design-deviation` | `CKPT "$SPIKE_ID" --deviation` — if it reports the cap, `CKPT "$SPIKE_ID" --terminal design-deviation-cap` + release + surface + STOP; else re-dispatch the designer with `DEVIATION_PATH` set, then the executor |
| `aborted-scope` | `CKPT "$SPIKE_ID" --terminal aborted-scope`; release; STOP |

### Step 2b — Derive the verdict (deterministic)

```bash
python3 "$WS/.claude/scripts/spike-decide.py" "$SPIKE_ID"   # writes decision.json
CKPT "$SPIKE_ID" decided                                    # gate: hashes + canonical verdict
```
If `spike-decide.py` exits non-zero (a measurement/criterion inconsistency — e.g. a non-numeric value under a numeric operator), it did NOT write decision.json. That is an executor data error: `CKPT "$SPIKE_ID" --deviation` and re-run the executor (or surface if the cap is hit). Proceed to Step 3 only after `decided`.

---

### Step 3 — Dispatch spike-writer

Header:
```
SPIKE_ID={SPIKE_ID}
DESIGN_JSON_PATH={DESIGN_JSON_PATH}
MEASUREMENTS_PATH={MEASUREMENTS_PATH}
DECISION_PATH={DECISION_PATH}
POC_DIR={SPIKE_DIR}/poc
NOTE_PATH={NOTE_PATH}
```

On return (`complete`): `CKPT "$SPIKE_ID" written` (re-dispatch once on refusal). Then:

- **`--skip-review`**: `CKPT "$SPIKE_ID" --terminal skip-review` (requires the canonical verdict, which exists — decision.json was derived) → emit the Phase 5 artifact receipt described below → `bash "$WS/.claude/scripts/spike-release.sh" "$SPIKE_ID"` → print the summary (id, derived verdict, note path, ingestion receipt) → STOP.
- otherwise: proceed to Step 4.

---

### Step 4 — Dispatch spike-reviewer

Hand off the decision + note hashes (the reviewer echoes them):
```bash
DECISION_HASH=$(CKPT "$SPIKE_ID" --get decision_hash)
NOTE_HASH=$(CKPT "$SPIKE_ID" --get note_hash)
```

Header:
```
SPIKE_ID={SPIKE_ID}
SPIKE_DIR={SPIKE_DIR}
DECISION_HASH={DECISION_HASH}
NOTE_HASH={NOTE_HASH}
REVIEW_JSON_PATH={REVIEW_JSON_PATH}
REVIEW_PATH={REVIEW_PATH}
VALIDATE_SCRIPT={WS}/.claude/scripts/spike-validate.py   # substitute the resolved absolute $WS path
```

On return (`complete`): `CKPT "$SPIKE_ID" reviewed`. If this refuses because the echoed `note_hash`/`decision_hash` is stale (the review was of an old note), re-dispatch the reviewer once with fresh hashes. Then read the verdict:
```bash
REVIEW_VERDICT=$(CKPT "$SPIKE_ID" --get review_verdict)
```

| REVIEW_VERDICT | Action |
|---|---|
| `ACCEPT` | `CKPT "$SPIKE_ID" complete` (the gate requires ACCEPT + a canonical verdict — it cannot pass otherwise). Emit the Phase 5 artifact receipt described below. `bash "$WS/.claude/scripts/spike-release.sh" "$SPIKE_ID"`. Print completion summary (derived verdict + review verdict + ingestion receipt). STOP. |
| `RE-RUN` | `CKPT "$SPIKE_ID" --rerun`. If it reports the cap → `CKPT "$SPIKE_ID" --terminal rerun-cap` + release + surface + STOP. Else return to **Step 1** (designer → executor → decide → **writer** → reviewer — the note IS regenerated). If the reviewer identified a design flaw, pass `DEVIATION_PATH`. |
| `RECONSIDER-DECISION` | `CKPT "$SPIKE_ID" --reconsider`. If cap → `CKPT "$SPIKE_ID" --terminal reconsider-cap` + release + surface + STOP. Else return to **Step 3** (writer only; same measurements + derived verdict, new note). |
| empty / other | The reviewer returned but review.json/verdict was malformed. `CKPT "$SPIKE_ID" --terminal reviewer-malformed`; release; print an inspection-required error; STOP. Do NOT default-route. |

On this re-dispatch of the writer (RECONSIDER), pass the `fast-mechanical` class's first fallback model as the hint — re-running the same small model on identical inputs risks repeating the same slip.

### Phase 5 artifact receipt (successful terminals only)

After `complete` or audited `skip-review`, and before releasing the lock, run:
```bash
if [ -f "$WS/scripts/artifact_skill_capture.py" ]; then
  python3 "$WS/scripts/artifact_skill_capture.py" emit \
    --workspace "$WS" --producer spike --run-id "$SPIKE_ID" \
    --root "$(git rev-parse --show-toplevel)/$SPIKE_DIR" --apply
fi
```
Do not emit a receipt for aborted, capped, malformed, `RE-RUN`, or `RECONSIDER-DECISION` states.
The receipt inventories the policy-eligible documents only; POC source is not captured. It writes
to neither Qdrant nor Graphiti and keeps Graphiti bulk ingestion disabled. Capture is best-effort:
never change state, delete evidence, or withhold lock release to repair it. Report
`ingestion receipt: created|idempotent|unavailable|failed`.

---

## State model (state.json, phase-driven)

Resume and completion come from `state.json` (`CKPT <id> --get phase`), never from which files exist. A half-written `review.md` no longer reads as "review complete" — the phase only advances when `spike-checkpoint.py` accepts the evidence. The hash chain additionally refuses a stale generation (an out-of-band edit to design.json after `designed` makes `executed` refuse). Full schema: `spike-state-schema.md`.

---

## Anti-pattern guard

| Tempting belief | Reality |
|---|---|
| "Skip the designer — I'll describe the experiment inline." | The typed criteria in design.json ARE the verdict contract. Without them there is nothing for spike-decide.py to derive from. |
| "The POC code is good enough to keep." | Stop the spike and open a milestone. POC is sandboxed and throwaway. |
| "RE-RUN can skip the writer." | NO. RE-RUN regenerates measurements → a new decision → a **new note** → review. The hash chain enforces this: a review echoing a stale `note_hash` is refused at `reviewed`. |
| "The writer picks the verdict." | NO. The verdict is DERIVED by spike-decide.py into decision.json. The writer cites it. |
| "UNCERTAIN is a failure to reach a verdict." | UNCERTAIN with a named methodology gap (an executor `{"unmeasured": true, "reason": …}`) is a valid, valuable result. |
| "I can auto-update the roadmap on a NO." | NO. The orchestrator NEVER mutates roadmap files. Surface the implications; the user owns the roadmap. |
| "The lock self-releases if the session dies." | It does NOT. Recover with `spike-release.sh <id>` (see Recovery). |
| "I can hand-edit state.json to get past a gate." | NO. state.json is spike-checkpoint.py's. Fix the artifact the gate is complaining about. |

---

## External-write boundary

This pipeline does NOT write to GitLab, Confluence, Jira, or any AWS resource. All artifacts are local under `.claude/notes/spikes/<id>/`. If the implications require an external write, surface it to the user and wait for explicit confirmation per the workspace CLAUDE.md "External System Write Policy" — the pipeline itself does not execute those writes.

---

## Sub-agent contract

Every sub-agent returns the JSON in `data/references/spike-agent-contract.md`. The orchestrator validates the shape, then **runs the checkpoint advance as the authoritative gate** — it never routes on `status` alone, and never hand-writes an artifact or state.json to get past a refusal. One agent per phase, one turn (the pipeline is sequential).

---

## Recovery — stuck lock

The coarse lock `.claude/notes/spikes/.lock` (one spike at a time, system-wide) does NOT self-release. Recover:
```bash
bash "$WS/.claude/scripts/spike-release.sh" <spike-id>            # normal (id matches)
bash "$WS/.claude/scripts/spike-release.sh" <spike-id> --force    # steal (held by another id)
```
It is idempotent (no lock → `no lock to release`, exit 0). After releasing, resume with `--resume` (continues from the recorded phase) or run fresh.

---

## Project-local cadence

- **Spike artifacts are LOCAL-ONLY — gitignored, NOT committed** (state.json, the JSON artifacts, poc/, note.md, review.md). CI Gate 1d fails if a spike state.json is ever tracked. To share a conclusion, promote it into the roadmap or a memory tier via `/memory-sync` — those ARE committed.
- The lock and state files are session-local orchestrator state.

---

## Files in /spike

```
.claude/notes/spikes/<id>/
├── state.json             # machine state (spike-checkpoint.py)
├── design.json/.md        # Phase 1 (designer)
├── poc/                   # Phase 2 sandbox (executor)
├── measurements.json      # Phase 2 (executor)
├── design-deviation.md    # Phase 2 (executor, if flawed)
├── decision.json          # DERIVED verdict (spike-decide.py)
├── note.md                # Phase 3 (writer)
└── review.json/.md        # Phase 4 (reviewer)

data/scripts/                   # flat naming (MCP discovery)
├── validate-spike-id.sh        # id shape check
├── spike-init.sh               # atomic coarse lock + create state.json
├── spike-checkpoint.py         # phase machine (advance/get/set/rerun/reconsider/deviation/terminal)
├── spike-validate.py           # artifact schema validator
├── spike-decide.py             # deterministic verdict deriver
├── spike-release.sh            # release the coarse lock
└── spike-status.sh             # list / detail from state.json

data/references/
├── spike-protocol.md           # what a spike IS
├── spike-state-schema.md       # state.json schema + single-writer rules
├── spike-artifact-schema.md    # design/measurements/decision/review JSON schemas
├── spike-agent-contract.md     # per-agent return shape + validate-the-pointer rule
└── spike-note-template.md      # note.md skeleton

data/agents/
├── spike-designer.md · spike-executor.md · spike-writer.md · spike-reviewer.md
```
