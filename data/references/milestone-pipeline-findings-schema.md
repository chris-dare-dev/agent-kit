---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---
# Findings register schema (v1) — `.claude/notes/milestones/<ID>/findings.json`

The machine-readable companion to the Phase 3 critique files (the per-finding
analog of the roadmap milestone register — `roadmap-milestones-schema.md` is the
pattern this mirrors). One object per finding, extracted deterministically from
the critique markdown (format v1.0 — `milestone-pipeline-critique-format.md`),
so Phase 4 can enforce "every CRITICAL/HIGH fixed-or-invalidated before
`code-complete`" instead of reading prose. The critique **files** stay canonical for
prose (What/Why/Proposed fix); the register is canonical for **status**.

**LOCAL-ONLY — NEVER COMMITTED (policy lock-in 2026-07-09, owner: Chris Dare).**
The register lives inside the milestone state dir (`.claude/notes/` tier), which
`ensure-claude-gitignore.sh` (called by every pipeline init) excludes in the
target repo — via `.git/info/exclude` since 2026-07-09 (round-4 proc-9; never by
mutating the tracked `.gitignore`; repos with a previously-committed block keep
it). Do not commit it, do not move it into a tracked path. CI in
agent-kit runs the tooling self-tests only.

**Who writes what (single-writer rule — the anti-drift discipline):**

| Field class | Single writer | When |
|---|---|---|
| Object creation, `id`, `severity`, `critic`, `file`, `line`, `title`, `regression_guard`, `critique_file` | `milestone-pipeline-findings.py extract` — deterministic parse of the critique file(s); **fail-loud on malformed blocks (exit 1 listing them), never a silent skip** | Once, after Step 3 dedupe; re-extract re-derives structure while preserving status/resolution/history by id, and **refuses drops** of registered ids |
| `status`, `resolution`, `history[]` | `milestone-pipeline-findings.py set` (flock'd, typed, audited) | Phase 4, per finding disposition |

No other writer is sanctioned. The critics never write JSON (they author the
markdown and self-lint it with `extract --check`); the rectifier and the
orchestrator call `set`. Hand-edits are unsanctioned — if one is ever needed,
re-run `extract` afterward and expect the drop-refusal to catch deletions.

## File shape

```json
{
  "schema_version": 1,
  "milestone_id": "ISSUE-1234",
  "critique_files": [
    "docs/ISSUE-1234_adversary_critique.md",
    "docs/ISSUE-1234_infra_critique.md"
  ],
  "generated_by": "milestone-pipeline-findings.py extract",
  "generated_at": "2026-07-09T00:00:00Z",
  "updated_at": "2026-07-09T01:00:00Z",
  "findings": [
    {
      "id": "H1",
      "severity": "HIGH",
      "critic": "adversary",
      "file": "charts/kiali/templates/secret.yaml",
      "line": 12,
      "title": "Missing namespace selector",
      "regression_guard": "charts/kiali/tests/test_multicluster_secret.yaml",
      "critique_file": "docs/ISSUE-1234_adversary_critique.md",
      "status": "fixed",
      "resolution": "ab12cd3",
      "history": [
        {"at": "2026-07-09T00:00:00Z", "from": null, "to": "open", "reason": "extracted"},
        {"at": "2026-07-09T01:00:00Z", "from": "open", "to": "fixed", "reason": "ab12cd3"}
      ]
    }
  ]
}
```

## Field reference

Top level:

| Key | Type | Rule |
|---|---|---|
| `schema_version` | int | Must be `1`. **Writers refuse any other value** (see Versioning below) |
| `milestone_id` | string | The run's milestone id (state.json's `id`) |
| `critique_files` | array | Every critique file the register was extracted from — pass ALL of the run's files to `extract`, never a subset |
| `generated_by` / `generated_at` / `updated_at` | string | Provenance; UTC `%Y-%m-%dT%H:%M:%SZ` |
| `findings` | array | One object per finding, shape below |

Per finding:

| Key | Type | Rule |
|---|---|---|
| `id` | string | The critique's finding id (`C1`/`H2`/`M1`/`L1`, `V-*` delivery-integrity, `F-*` frontend, `I-*` infra, `O-*` oss-scout). Unique across the run's critique files |
| `severity` | enum | `CRITICAL` \| `HIGH` \| `MEDIUM` \| `LOW` — must agree with the id's letter (extract refuses mismatches) |
| `critic` | string | From the block's `**Source critic:**` line |
| `file` / `line` | string-or-null / int-or-null | Best-effort machine citation: first backticked token of the `File:` line + first integer of its `:N` suffix. Null for `File: n/a (...)` citations. The prose `File:` line in the critique stays the rectifier's read surface |
| `title` | string | The finding header's title |
| `regression_guard` | string-or-null | From the block's `**Regression-guard:**` line |
| `critique_file` | string | Which critique file the finding came from (drives per-file count reconciliation) |
| `status` | enum | `open` \| `fixed` \| `deferred` \| `invalidated` |
| `resolution` | string-or-null | fixed: rectification commit sha; deferred: deferral reason; invalidated: re-verification note. **REQUIRED by `set` for every transition** |
| `history` | array | Append-only transition log: `{"at": <utc>, "from": <status-or-null>, "to": <status>, "reason": <string>}` |

## Status state machine (enforced by `milestone-pipeline-findings.py set`)

```
open ──→ fixed          (terminal)
  │  └─→ invalidated    (terminal)
  └────→ deferred ──→ fixed
```

- Forward-only. `fixed` and `invalidated` are terminal; `deferred → fixed` is
  the single second edge (a deferral later upgraded to a fix in the same run).
  There is no reopen — a genuinely wrong terminal set is v2 material (see
  Won'ts); until then the audited `history[]` is the correction record.
- Setting the **current** status again is an idempotent no-op (exit 0) —
  resume-safe.
- `--resolution` is required on every `set` — a deferral without a reason or a
  fix without a commit sha is exactly the hand-waving this register retires.
- **Concurrency:** exclusive `fcntl` flock on `<findings.json>.lock` around
  every read-modify-write; atomic replace.

## The gate (Phase 4 → `code-complete`)

`milestone-pipeline-findings.py gate <ID>` exits **3** while any CRITICAL/HIGH
finding is `open`, and **warns** (exit 0) about open MEDIUM/LOW findings
(severity-scope decision: Chris Dare, 2026-07-09 — C+H block; M/L deferral is
audited via `set ... deferred --resolution`, not gated, because a gate cannot
measure the ≤30-LOC MEDIUM rule).

**One authority, two invocation points:** the gate logic lives ONLY in the
findings script. `milestone-pipeline-checkpoint.py` subprocess-invokes it at
`code-complete`, and the command runs it before closure. V2 requires a
repo-relative `state.findings_register`; absence, path escape, milestone-id
mismatch, or critique-set mismatch refuses. There is no legacy/ad-hoc skip in
the v2 writer—legacy state must migrate explicitly.

## Versioning & migration policy

`schema_version` is frozen at `1`. Writers (`extract` merge path, `set`) REFUSE
files with any other version rather than guess. A future v2 ships as an
explicit migration script plus a bump in the same commit — never as writers
that silently tolerate both shapes. (Copied from the roadmap register policy.)

## Relationship to the other artifacts

| Artifact | Relationship |
|---|---|
| `.claude/notes/milestones/<ID>/artifacts/reviews/*-critique.md` | Per-task prose canon. `extract` parses it; `extract --check` is the format lint the critics self-run; per-file `Severity counts:` lines must match the register (reconcile checks) |
| `.claude/notes/milestones/<ID>/state.json` | Carries the exact register pointer and summary projections; `code-complete` validates identity/set and the review/implementation artifacts bind its file hash |
| `artifacts/review-manifest.json` / `implementation-evidence.json` | Bind the exact critique set, body/prompt/critique hashes, register hash, findings-gate result, and independent closure report |
| Roadmap register (`.claude/notes/roadmaps/<slug>/milestones.json`) | Sibling regulated artifact, one level up (milestone-granularity vs finding-granularity) |
| `pipeline-reconcile.py` | `findings-sync`: register ↔ critique files ↔ state arrays ↔ counts, scanned per milestone state dir (ad-hoc runs included) |

## Tooling

| Invocation (via `"$WS/.claude/scripts/milestone-pipeline-findings.py"`, always `python3`) | Purpose |
|---|---|
| `extract --check <critique.md>...` | Format lint only (critics self-check before returning; CI-style validation). Exit 0 clean / 1 listing every problem |
| `extract --id <ID> <critique.md>...` | Parse → findings.json (merge-safe, drop-refusing). Pass EVERY critique file of the run |
| `set <ID> <ids> <status> --resolution "..."` | The ONLY status writer; comma-list ids supported |
| `gate <ID>` | Exit 3 on open C/H; WARN on open M/L |
| `summary <ID> [--field K] [--counts-for FILE]` | Derived counts + status arrays for the state.json `--set`s |
| `dedupe <critique.md>` | Intra-file nearby-finding clustering only (not cross-file reconciliation) |
| `--self-test` | Fixtures for every refusal path (CI Gate 1c) |

## Won'ts (design locks, v1)

- **Never committed to git** — see the policy block at the top.
- **No YAML.** Engineer machines run stdlib python3; JSON keeps every consumer
  dependency-free.
- **No What/Why/Proposed-fix prose duplicated into the JSON** — prose lives
  once, in the critique file. The register carries only what gates and
  reconciliation need.
- **No agentic enforcement of these invariants** — deterministic scripts block;
  critics/rectifier advise and author prose. No LLM "lint agents".
- **No reopen edge (v1).** A wrongly-terminal status is rare enough that v1
  ships without it; if practice proves otherwise, v2 adds an audited reopen
  mirroring the roadmap register's `cancelled → pending`.
- **No cross-file dedupe clustering (v1).** `dedupe` clusters within one file
  (the historical behavior); critics currently write separate files, so
  cross-critic agreement across files is future work — data-gate it.
- **No backward-compatible absence in delivery-state v2.** V1 state is migrated
  explicitly and cannot advance to code-complete until a current register,
  hash-bound reviews, and independent closure exist.
