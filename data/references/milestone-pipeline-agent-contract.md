---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---
# Agent return contract — what each milestone-pipeline subagent returns

The single home for the per-agent return shape. Each agent body's "Return
value" / "Output" section restates its own contract for self-containment; THIS
file is what the **orchestrator** validates against before routing, so a drift
between an agent body and this file is a bug (fix the agent body — this file is
canonical for the contract's *shape*; the agent bodies are canonical for how
the work gets done).

## Why doc-level, not a validation script (decision record, 2026-07-09)

Claude Gen-1 and the Codex skill adapter both receive free-text subagent return
messages; neither makes that message a delivery authority. Codex may lack a
named custom-agent selector, so its adapter also persists the exact prompt and
binds a per-run snapshot of the canonical agent body. This avoids ambient
catalog updates invalidating an in-flight review. A task name alone never
proves which role ran, and current Codex task ids are provenance attestations,
not runtime-signed receipts.

The current Codex collaboration API returns a canonical task name, not a
separate opaque id. The orchestrator chooses a globally unique path-safe leaf,
verifies the returned canonical name ends in that exact leaf, and records the
unchanged leaf as `agent_task_id`. This preserves filename safety without
pretending that a sanitized or invented value came from the runtime.

The artifacts themselves ARE machine-validated, at the artifact layer:
critiques via `milestone-pipeline-findings.py extract --check`, review identity
and receipts via `review-manifest.json`, delivery artifacts via
`milestone-pipeline-artifacts.py`, state via `checkpoint.py`, and findings via
the register tooling. The return message is only a pointer.

## The orchestrator's validation rule (applies to every dispatch)

On each subagent return:

1. **Check the shape** against the agent's contract below (required items
   present, paths are repo-relative or absolute as specified, no artifact body
   echoed into the message).
2. **Check the pointed-at artifact exists** (the brief file, the critique
   file, the commits via `git rev-parse`) before checkpointing anything.
3. On violation: **re-dispatch ONCE**, quoting the missing/malformed items
   verbatim in the new prompt's header. A second violation is a hard stop —
   surface it to the user (a systematically non-conforming agent body is a
   prompt bug to fix, not to absorb).
4. Never repair a violation by inferring what the agent "meant" — the
   contract exists so the orchestrator does not have to guess.

## Per-agent contracts

### milestone-researcher (Phase 1; also Phase 3 oss-scout mode)

Returns a SINGLE message:
1. The path to the written brief (`BRIEF_PATH` as dispatched; oss-scout mode:
   `docs/{ID}_oss_scout.md`).
2. A 3-line summary: recommended approach, main risk, one key finding.

MUST NOT echo the brief body. Violation examples: no path; summary longer than
~5 lines; brief content pasted into the message.

### milestone-implementer (Phase 2, delegated path)

Returns a SINGLE message:
1. Branch name committed on.
2. Commit shas (list).
3. Validation results — subsystem + exit code per check.
4. Files touched (paths).
5. Delivery actions expected (publication and per-target operations), for the
   main session to model in the release manifest / frozen operations plan; this
   is not an authorization receipt.
6. 5-line summary of what was actually implemented.

MUST NOT echo implementation details/diffs. If a validation step required an
external write, the message says so explicitly and the step was NOT performed.

### milestone-adversary / milestone-delivery-integrity-adversary / milestone-frontend-ux / milestone-infra-safety (Phase 3)

Each returns ONLY:
1. The path to the written critique (`CRITIQUE_PATH` as dispatched).
2. A 3-line summary: severity counts (prefixed per critic: none / `V-` / `F-` /
   `I-`), headline finding, verdict.
3. The result line of the format self-check it ran before returning
   (`milestone-pipeline-findings.py extract --check <critique>` — `check: OK`
   or the failure it could not fix within 2 attempts).

MUST NOT echo the critique body. A critic that returns without the self-check
line gets the single re-dispatch; a critic whose self-check FAILED unresolved
is surfaced to the user before Step 3's `extract` (which would refuse anyway —
fail-loud, never skip).

### milestone-rectifier (Phase 4, exception path only)

Returns a rectification summary: milestone id, implementation commits,
rectification commit sha, fixed/invalidated/deferred finding ids,
regression-guard tests added, project-check result, and delivery actions still
pending. Also: the findings-register
dispositions it recorded via `milestone-pipeline-findings.py set`, or the
structured `needs-specialist` JSON tag when a finding needs domain expertise
(see the agent body).

MUST NOT push, create MRs, or execute any external write — the message lists
them as pending.

### milestone-closure-verifier (after rectification, before code-complete)

Returns only:

1. The written closure report path.
2. `PASS` or `FAIL`.
3. One-line reason.

The report binds the final range and hashes of the findings snapshot, assessment
manifest, active passing checks, and complete append-only check-attempt ledger,
plus every rerun command/exit code. A
missing input or unverifiable C/H disposition is `FAIL`. The verifier never
edits code or findings.

### milestone-operations-adversary (after publication, before apply-running)

Returns only the operations report path, `PASS` or `FAIL`, and a one-line
reason. It reviews immutable release/plan snapshots and never runs plan
commands. The orchestrator writes its task-specific receipt and appends it only
through `review-append`; every failed attempt remains in `operations_reviews[]`.

## Cross-references

- Dispatch-prompt headers: `data/commands/milestone-pipeline.md`.
- Agent bodies: `data/agents/milestone-{researcher,implementer,adversary,delivery-integrity-adversary,frontend-ux,infra-safety,rectifier,closure-verifier}.md`.
- Artifact validation: `milestone-pipeline-findings-schema.md`, `milestone-pipeline-state-schema.md`, `milestone-pipeline-artifacts-v2.md`.
