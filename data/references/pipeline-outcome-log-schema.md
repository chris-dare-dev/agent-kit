---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Reference — pipeline-outcome-log schema + emit map (E1 / KR1 capture layer)

The **foundational capture layer** of the self-improving-tooling loop (roadmap
`self-improving-tooling`, milestone `self-improving-tooling-m1`, epic E1, KR1). At each
pipeline's terminal/`complete` transition, one labelled JSON record is appended to a single
append-only JSONL log. This is the dataset later milestones read (E3 learning, E5 sealed-eval).

**M1 is CAPTURE ONLY** — it does not learn, feed back, or change any pipeline behavior. Every
emit is **additive + best-effort**: a writer failure can never abort the host pipeline's
terminal transition.

- **Writer/reader:** `data/scripts/pipeline-outcome-log.py` (Python stdlib only, no deps)
- **Tests:** `data/scripts/pipeline-outcome-log-test.py` (`python3 …` — 13 tests, all stdlib)

---

## Log path (local-only, gitignored)

Resolution order (first match wins):

1. `--log <path>`
2. `$PIPELINE_OUTCOME_LOG` env var
3. `<repo-root>/.claude/notes/pipeline-outcomes/outcomes.jsonl` (default)

`.claude/notes/` is excluded in every target repo by `ensure-claude-gitignore.sh` (via
`.git/info/exclude` since 2026-07-09), so the
log is **local-only by construction** (KR1: "per-machine, never pushed"). The only external
write in M1 is the `git push origin main (agent-kit)` of the *script + this doc + the
wiring* — never the log itself. Repo-root detection mirrors `milestone-pipeline-checkpoint.py`
(`$REPO_ROOT` → `$PLATFORM_ROOT` → `git rev-parse --show-toplevel` → walk-up to `.git/`).

---

## Record schema (JSONL — one object per line)

`schema_version` first for forward-compat. **Every field is present in every record**; a field
a family lacks is emitted as `null` (NOT omitted), so readers `dict.get`/`jq`/pandas see a
stable column set.

```json
{
  "schema_version": 1,
  "pipeline": "milestone",
  "id": "self-improving-tooling-m1",
  "run_id": null,
  "status": "complete",
  "verdict": null,
  "rectification_count": 2,
  "rectification_commit": "ab12cd3",
  "critique_critical": 0,
  "critique_high": 2,
  "critique_medium": 4,
  "critique_low": 5,
  "candidate_count": null,
  "spike_review_verdict": null,
  "token_cost": null,
  "started_at": "2026-06-17T14:32:00Z",
  "completed_at": "2026-06-17T15:47:00Z",
  "emitted_at": "2026-06-17T15:47:01Z",
  "source_state_path": ".claude/notes/milestones/self-improving-tooling-m1/state.json"
}
```

### Field → source map per family

| Field | Type | milestone (A) | discovery ×6 (B) | spike (C) | roadmap | argoops |
|---|---|---|---|---|---|---|
| `schema_version` | int | `1` const | `1` | `1` | `1` | `1` |
| `pipeline` | str | `"milestone"` | family slug | `"spike"` | `"roadmap"` | `"argoops"` |
| `id` | str | `state.id` | `state.id` | spike-id (`--field`/`--id`) | `<slug>` | ISO-ts (argoops has no per-run id) |
| `run_id` | str\|null | `null` | `null` | `null` | `null` | `null` |
| `status` | str | `state.phase` | `state.phase` | terminal (`--field status=`) | `complete` | `complete` |
| `verdict` | str\|null | `null` | `null` | spike YES/NO/UNCERTAIN (`--field verdict=`) | `null` | `null` |
| `rectification_count` | int\|null | `--field` (rectifier fixed-count) | `null` | `null` | `null` | `null` |
| `rectification_commit` | str\|null | `--field` (rectification commit SHA) | `null` | `null` | `null` | `null` |
| `critique_{critical,high,medium,low}` | int\|null | `--field` (adversary critique C/H/M/L) | `--field` (workflow `challenge_counts.{k}`) | `null` | `null` | `null` |
| `candidate_count` | int\|null | `null` | `--field` (workflow `candidate_count`) | `null` | `null` | `null` |
| `spike_review_verdict` | str\|null | `null` | `null` | reviewer ACCEPT/RE-RUN/RECONSIDER-DECISION (`--field`) | `null` | `null` |
| `token_cost` | num\|null | `null` (M1) | `null` | `null` | `null` | `null` |
| `started_at` | iso\|null | `state.created_at` | `state.created_at` (seed-time) | `null` | `null` | `null` |
| `completed_at` | iso\|null | `state.updated_at` | `state.updated_at` (seed-time) | `null` | `null` | `null` |
| `emitted_at` | iso | writer `now()` | `now()` | `now()` | `now()` | `now()` |
| `source_state_path` | str\|null | state.json path | state.json path | `null` (or `--field`) | `plans/<slug>-roadmap.md` (`--field`) | `null` |

**Severity-counts source — `--field`, NOT `state.json`, on the live path.** The writer *can*
map a `critique_finding_counts` (milestone) / `challenge_finding_counts` (discovery) sub-dict
out of `state.json` (it prefers whichever is present; mutually exclusive across the two
families) — but on the **live path neither sub-dict is ever written to `state.json`**: the
discovery `*-workflow.mjs` returns `candidate_count` + `challenge_counts` only in its JS return
object to the main session, and the milestone orchestrator never `--set`s `critique_finding_counts`.
So the slash command supplies these columns via `--field` from its in-scope values (the
workflow return object for discovery; the adversary critique counts + rectifier fixed-count /
commit for milestone), which **win over `--state`**. `--state` on these two families therefore
only fills timestamps (and those are seed-time for discovery — see below). If neither a
`--field` nor a populated sub-dict is present, the columns are `null` (nulls-not-guesses).

**Discovery `started_at`/`completed_at` are seed-time, not phase-accurate.** The discovery
`*-workflow.mjs` never touches `state.json`, so `created_at == updated_at == init time` →
a zero-duration run. The wiring deliberately does **not** guess a completion time; `emitted_at`
(writer `now()`) is the de-facto completion marker. A future "how long do scouts take" feature
must read `emitted_at`, not the seed-time `started_at`/`completed_at` (or the wiring should be
extended to pass `--field completed_at=<now>` for discovery). This is the nulls-not-guesses
principle applied to durations.

### Derived / intentionally-null fields

- **`rectification_count`** — milestone `state.json` has no integer count. **LOCKED DECISION
  (asserted in `TestRectificationCount`): `len(fixed_findings)`.** If a future schema adds an
  explicit count, prefer it (and bump per the rule below).
- **`token_cost`** — intentionally `null` in M1. Token cost lives in the MCP server's
  `token-stats.ts`, not `state.json`; emitting it is E4's OTel job (spike-gated). The field
  exists now so E4 can backfill without a schema bump. If a `token_cost` key ever lands in
  `state.json`, the writer reads it.
- **`verdict` vs `spike_review_verdict`** — two distinct spike signals, both kept:
  `verdict` = the spike's YES/NO/UNCERTAIN assumption answer (from `note.md`);
  `spike_review_verdict` = the reviewer's ACCEPT/RE-RUN/RECONSIDER quality gate (from
  `review.md`).

### `schema_version` bump rule

- Additive **nullable** field → **NO bump** (readers `.get`).
- Renamed / removed / **retyped** field → **bump to 2** + the `summary` reader switches on
  `schema_version`.

---

## Best-effort metric emit (E4 — self-improving-tooling-m4)

`emit()` performs an **additive, best-effort** metric push AFTER the authoritative JSONL
append (`_emit_metric(record)`). It is non-blocking by construction: stdlib `urllib` only,
5 s timeout, and an inner `try/except` that swallows **every** error (network, auth, config)
so a metric failure can never abort the host pipeline's terminal transition or corrupt the
JSONL record. Asserted by `TestMetricEmitBestEffort`.

**Credential-agnostic interface** (the auth is realized off-script — Istio RA+AP on the
ingress gateway + a Keycloak `pushgateway-emitter` client + an L5-Apps SM secret):

| Env var | Required | Meaning |
|---|---|---|
| `PIPELINE_METRICS_GATEWAY` | yes (else no-op) | Base URL incl. host, e.g. `https://pipeline-metrics.auth.example.com` (the DEDICATED metrics-ingest subdomain). The pushgateway push path `/metrics/job/...` is appended. |
| `PIPELINE_METRICS_TOKEN` | yes (else no-op) | Bearer token for the `Authorization` header. Populated out-of-band by an SM-fetch wrapper (laptop: `YOUR_AWS_PROFILE`; CI: SM) that mints a Keycloak client_credentials token for the `pushgateway-emitter` client. The script is indifferent to HOW it is sourced. |
| `PIPELINE_METRICS_ENV` | no (default `dev`) | The `env` metric label. |

When either required var is absent the push is **skipped silently** — callers without these
env vars behave exactly as before this milestone landed.

**Metric shape** (Gauge; bounded labels only). The name carries NO `_total` suffix —
that suffix is reserved by Prometheus convention for monotonic counters, and a point-in-time
token-cost-per-run is a Gauge (m4 rectification M2/I-L1):

```
# TYPE claude_run_token_cost gauge
claude_run_token_cost{spike_id="<record.id>",env="<env>",run_bucket="<4hex>"} <token_cost>
```

POST path (grouping key — bounded labels only; `run_id` NEVER appears in the URL). The
dedicated host routes `/` straight to the pushgateway push API, so the path is the canonical
`/metrics/job/...` (no `/ingest/` segment — the whole host IS the ingest endpoint):
`/metrics/job/claude-pipeline-run/env/<env>/run_bucket/<bucket>`.

> Note: this final metric/label shape (`claude_run_token_cost{spike_id,env,run_bucket}`)
> supersedes the cardinality-brief's `pipeline_token_cost_total{pipeline,...}` sketch
> (owner-locked decision). `spike_id` (= `record.id`, the cross-family run identifier:
> milestone id / spike id / roadmap slug) replaced the brief's `pipeline` label.

**`run_bucket`** = first **4 hex** chars of `sha256(run_id)` (`_run_bucket()`) → at most
`16**4 = 65536` distinct series regardless of run volume. This is the LOCKED cardinality
scheme (m4): exemplars were rejected because **Thanos does not federate exemplars**, and an
unbounded `run_id` label explodes Prometheus series. **The JSONL `run_id` field stays the
authoritative exact join key**; the bounded metric is for Grafana/Thanos trend exploration
only. Asserted by `TestRunBucket`.

---

## Writer / reader CLI

```bash
# Families A/B (have a state.json): read it + map keys.
pipeline-outcome-log.py emit --pipeline milestone --id <ID> --state <path/to/state.json>

# Families C/roadmap/argoops (no state.json): pass scalars via repeatable --field key=value
# (value JSON-parsed, plain-string fallback — same convention as checkpoint.py --set).
pipeline-outcome-log.py emit --pipeline spike --id <id> \
    --field spike_review_verdict=ACCEPT --field verdict=yes --field status=accept

# --state + --field compose: state fills what it has, --field overrides/fills the rest.

# Read/summary (the surface E3/E5 read later). Tolerates malformed lines.
pipeline-outcome-log.py summary [--pipeline <fam>] [--last N] [--json] [--log <path>]
```

`emit` is **exit-0-on-internal-error** (best-effort; never aborts the host pipeline) — even on
bad args. `summary` may exit non-zero on bad args (diagnostic, off the critical path).

---

## Concurrency mechanism (guarantee a)

**`O_APPEND` + one `os.write()` of a single compact (`< PIPE_BUF`) line** — NOT `flock` for the
common case. POSIX guarantees a `write()` ≤ `PIPE_BUF` (4096 on Linux/macOS) to an `O_APPEND`
fd is atomic w.r.t. concurrent appenders, so two simultaneous emitters produce two intact,
non-interleaved lines. The record is compact-serialized (`json.dumps(separators=(",",":"))`,
~350–450 bytes), well under `PIPE_BUF`. **Guard:** a pathological oversized line (≥ `PIPE_BUF`)
falls back to an advisory `flock` around the write so the atomicity guarantee never silently
lapses. Proven by `TestConcurrency`: the thread test (50 writers × 20 iterations) **plus a
`multiprocessing` fork-pool test** (true OS-level parallelism, no GIL — the realistic case
since `/spike`, `/argoops`, `/milestone` are separate processes) + the oversized-line test.

---

## Emit-point map (S1.2) — 10 sites across 6 families

There is **no single shared `complete` choke point** across the families (their terminals
diverge). The design is **one shared writer script invoked from N thin call sites**, each
already an existing terminal step. Every call site is best-effort (`|| true` / the script's own
swallow). The discovery `*-checkpoint.py complete` path is **DEAD on the live Workflow path** —
do NOT wire there; wire at the slash-command present-report step.

| # | Family | Site | Mechanism |
|---|---|---|---|
| 1 | milestone | `data/commands/milestone-pipeline.md` — after `checkpoint.py "$ID" complete` | `--state .claude/notes/milestones/$ID/state.json` + `--field critique_{c,h,m,l}=` `--field rectification_count=` `--field rectification_commit=` |
| 2 | capability-scout | `data/commands/capability-scout.md` — Step 2 present-report | `--state .claude/notes/capability-scouts/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` (from workflow return) |
| 3 | mesh-as-code | `data/commands/mesh-as-code.md` — Step 2 present-report | `--state .claude/notes/mesh-as-code-runs/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` |
| 4 | interop-discovery | `data/commands/interop-discovery.md` — Step 2 present-report | `--state .claude/notes/interop-discoveries/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` |
| 5 | cicd-uplift | `data/commands/cicd-uplift.md` — Step 2 present-report | `--state .claude/notes/cicd-uplifts/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` |
| 6 | frontend-uplift | `data/commands/frontend-uplift.md` — Step 2 present-report | `--state .claude/notes/frontend-uplifts/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` |
| 7 | zerotrust-scout | `data/commands/zerotrust-scout.md` — Step 2 present-report | `--state .claude/notes/zerotrust-scouts/$ID/state.json` + `--field candidate_count=` `--field critique_{c,h,m,l}=` |
| 8 | spike | `data/scripts/spike-checkpoint.py` — at the terminal (advance to `complete`, or `--terminal <status>`), **exactly-once** via `state.json.outcome_emitted` | `--field verdict=` (the DERIVED YES/NO/UNCERTAIN from `decision.json`, mirrored in `state.json`) + `--field spike_review_verdict=` (reviewer gate from `review.json`) + `--field status=<terminal>` |
| 9 | roadmap | `data/commands/roadmap.md` — Step 4 when materializer returns `complete` | `--field status=complete --field source_state_path=plans/$SLUG-roadmap.md` |
| 10 | argoops | `data/scripts/argoops-release.sh` — after lock removal (end of every run) | `--id <ISO-ts>-$$ --field status=complete` (PID-disambiguated id) |

**Why these sites (per family):**

- **milestone (#1)** — the slash command has a real, live `checkpoint.py "$ID" complete`
  transition; the emit lines immediately after it. `--state` fills `id`/`status`/timestamps,
  but the orchestrator never persists `critique_finding_counts` / `fixed_findings` /
  `rectification_commit` to `state.json` — so the slash command passes the adversary critique
  C/H/M/L counts (`--field critique_*`), the rectifier's fixed-finding count
  (`--field rectification_count`), and the rectification commit SHA
  (`--field rectification_commit`) from its in-scope values. `--field` wins over `--state`.
- **discovery ×6 (#2–#7)** — the live terminal is the **slash-command present-report step**
  (the `*-workflow.mjs` ends at PRIORITIZE writing `final-report.md`; there is no live
  `checkpoint.py complete`). The emit fires once, after the report is presented. The live
  `*-workflow.mjs` **never writes `state.json`** — it returns `{ candidate_count,
  challenge_counts: { critical, high, medium, low } }` only in its JS return object to the
  main session — so the slash command supplies those via `--field candidate_count=` +
  `--field critique_{critical,high,medium,low}=` from the workflow return object (NOT from a
  `state.json` read). `--state` is kept only to fill seed-time timestamps; the seed file holds
  `candidate_count: 0` / `challenge_finding_counts: {0,0,0,0}` and must NOT be relied on for
  the counts.
- **spike (#8)** — the emit lives in `spike-checkpoint.py`, fired **exactly once** (guarded by
  `state.json.outcome_emitted`) at the terminal: the advance to `complete` (ACCEPT) or
  `--terminal <status>` (RE-RUN cap, RECONSIDER cap, `--skip-review`, brief-inadequate,
  aborted-scope, reviewer-malformed). It captures even NO/UNCERTAIN spikes — the most
  interesting training data. Two DISTINCT signals come straight from `state.json`, with **no
  markdown parsing** (the pre-v2 flow grep'd `note.md` for the verdict): `--field verdict=` is
  the **derived** YES/NO/UNCERTAIN (`spike-decide.py` → `decision.json`, mirrored into state),
  and `--field spike_review_verdict=` is the reviewer's ACCEPT/RE-RUN/RECONSIDER gate
  (`review.json`, mirrored into state) — kept distinct, never conflated. `--field
  status=<terminal>` labels each terminal so a RE-RUN-capped / aborted / brief-inadequate spike
  is distinguishable instead of a featureless row. `spike-release.sh` no longer emits — it only
  drops the lock; moving the emit into the checkpoint (under the state lock) is what makes it
  exactly-once.
- **roadmap (#9)** — the doc IS the state (no `state.json`); the terminal is the materializer
  returning `complete` in Step 4. `--field` supplies `status` + the roadmap doc path.
- **argoops (#10)** — `argoops-release.sh` is called at the end of every run (Phase 4). No
  per-run id exists, so the id is synthesized from an ISO timestamp.

**One missed emit = a silent downstream gap** in E3/E5. Each of the 10 sites is asserted by the
per-pipeline smoke (`TestPerPipelineEmitSmoke`), which also coverage-asserts all distinct
family slugs are present.
