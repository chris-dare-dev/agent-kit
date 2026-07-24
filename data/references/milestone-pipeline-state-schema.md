---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---

# Milestone pipeline delivery-state v2

`state.json` records orchestration state and hash-bound artifact pointers. It
does not duplicate evidence bodies and it does not let agent prose set delivery
status. The machine schema is
`data/schemas/milestone-pipeline-state-v2.schema.json`; semantic gates live only
in `data/scripts/milestone-pipeline-artifacts.py`.

V1/unversioned files are refused by every v2 writer. Use
`milestone-pipeline-migrate.py`; implicit or mixed migration is forbidden.

## Location

```text
<target-repo>/.claude/notes/milestones/<ID>/
├── state.json
├── state.v1.json                       # migration backup only
├── findings.json
├── research/
└── artifacts/
    ├── review-manifest.json
    ├── implementation-evidence.json
    ├── publication-intent.json
    ├── release-manifest.json
    ├── operations-plan.json
    ├── operations-evidence.json
    ├── waivers.json
    ├── checks/                         # every failed/successful check receipt
    └── reviews/                        # task-specific prompts/bodies/snapshots/reports
```

All artifact pointers are canonical, repo-local relative paths. Absolute paths,
`..`, and symlink escapes are refused. The checkpoint writer persists
`{path, sha256, generation, phase}` receipts in `artifact_bindings`; mutable
review/attempt/waiver artifacts additionally carry per-entry hashes so they may
only grow append-only.

## State shape

```json
{
  "schema_version": 2,
  "id": "ISSUE-1234",
  "created_at": "2026-07-12T12:00:00Z",
  "updated_at": "2026-07-12T13:00:00Z",
  "phase": "code-complete",
  "phase_history": [{"phase":"init","at":"2026-07-12T12:00:00Z"}],
  "agent_kit_commit": "<full canonical kit commit>",
  "kit_upgrade_history": [],
  "check_run_head": "987abcd",
  "check_run_hashes": {"artifacts/checks/current.json": "<sha256>"},
  "check_run_history": {},
  "check_run_attempts": [
    {"path": "artifacts/checks/current.json", "sha256": "<sha256>"}
  ],
  "milestone_brief": "verbatim user request",
  "research_mode": "standard",
  "research_briefs": [".../agent-a-brief.md", ".../agent-b-brief.md"],
  "research_synthesis": "...",
  "implementation_path": "inline",
  "implementation_specialist": null,
  "implementation_base": "abc1234",
  "implementation_commit_range": "abc1234..def5678",
  "implementation_commits": ["def5678"],
  "implementation_branch": "dev",
  "critique_path": ".claude/notes/milestones/ISSUE-1234/artifacts/reviews/adversary-task-1-critique.md",
  "critics_run": [
    "milestone-adversary",
    "milestone-delivery-integrity-adversary"
  ],
  "critique_files": [
    ".claude/notes/milestones/ISSUE-1234/artifacts/reviews/adversary-task-1-critique.md",
    ".claude/notes/milestones/ISSUE-1234/artifacts/reviews/delivery-task-2-critique.md"
  ],
  "critique_finding_counts": {"critical":0,"high":0,"medium":2,"low":1},
  "findings_register": ".claude/notes/milestones/ISSUE-1234/findings.json",
  "rectification_commit": "987abcd",
  "rectification_not_required_reason": null,
  "fixed_findings": ["M1"],
  "deferred_findings": ["L1"],
  "invalidated_findings": [],
  "regression_tests_added": ["tests/test_delivery.py"],
  "publication_required": true,
  "publication_not_required_reason": null,
  "operations_required": true,
  "operations_not_required_reason": null,
  "implementation_status": "validated",
  "operational_status": "pending",
  "review_status": "closed",
  "review_manifest": "artifacts/review-manifest.json",
  "implementation_evidence": "artifacts/implementation-evidence.json",
  "publication_intent": "artifacts/publication-intent.json",
  "release_manifest": "artifacts/release-manifest.json",
  "operations_plan": "artifacts/operations-plan.json",
  "operations_evidence": "artifacts/operations-evidence.json",
  "waivers": "artifacts/waivers.json",
  "artifact_bindings": {},
  "migration": null
}
```

`implementation_status`, `operational_status`, `review_status`, pointers,
bindings, timestamps, phase, schema version, check ledgers, the current
agent-kit commit, and its append-only upgrade history are machine-owned. `--set`
refuses them. A different executing kit may advance the frozen semantics only
through `kit-upgrade-preview` followed by exact-scope human authorization and
`kit-upgrade`; ordinary writers fail closed. Every human-authored
field group freezes when its owning phase closes; all `--set` calls are refused
at `complete`. Corrections use a new append-only attempt or milestone rather
than rewriting history.

## Legal transition graph

The state machine is an adjacency graph, not a numeric order:

```text
init -> research-running -> research-complete
     -> implement-running -> implement-complete
     -> critique-running -> critique-complete
     -> rectify-running -> code-complete

code-complete -> publish-running -> published
published -> plan-review-running -> plan-reviewed
plan-reviewed -> apply-running -> applied -> verify-running
verify-running -> operationally-verified -> complete

code-complete -> complete   only if publication and operations are explicitly not required
published -> complete       only if operations are explicitly not required
applied -> apply-running    append-only mutation retry
verify-running -> apply-running after a failed verification
operationally-verified -> verify-running for a live refresh
complete -> verify-running when required live evidence becomes stale
```

A retry never rewinds to implementation. Attempts advance within the frozen
target plan and retain the prior hash chain.

## Frozen review range: out-of-band rectification is not re-reviewable in place

The critique and rectify phases review the frozen `base..implementation_commits[-1]`
diff. `implementation_commits` is machine-owned and writable only at
`implement-running`; `--set` never rewrites it, and state.json is never hand-edited.
So a fix landed on the trunk *after* that frozen head is invisible to any in-state
re-review: re-running the adversarial assessment re-reads the pre-fix tree and
re-surfaces the already-fixed findings as open, not closed. Repairing a wedged
reviewer through `kit-upgrade` lets the re-run *bind*, but it still binds a review
of stale code.

Consequence: a milestone rectified out-of-band cannot be pipeline-re-run to
"confirm closure." Either (a) confirm closure by evidence-based attestation —
trace each finding to its landed fix, verify the fix is present in the current
`origin/main` tree, and confirm the milestone check plus kit `--self-test` are
green, recorded as a dated closure note; or (b) re-init under a fresh milestone id
whose implement range includes the fixes (usually disproportionate for already-
fixed findings). A wedged milestone may rest in `critique-running` under a
documented final disposition rather than being force-completed. The pipeline diff
is contiguous `base..head`, so no range cleanly captures a milestone plus later
interleaved trunk fixes. Observed on `kargo-sole-promoter-cutover-m3` (2026-07-20):
frozen head `40f58e4`, every rectification commit landed after it.

## Gate ownership

| Transition | Deterministic requirement |
|---|---|
| `research-complete` | nonempty research briefs + valid mode |
| `implement-complete` | base/range/commits/branch recorded |
| `critique-complete` | exact deterministic reviewer set, at least the two always-on blind adversaries, hash-bound prompts/bodies/critiques, findings register identity/set match |
| `code-complete` | findings gate has no open C/H, closure verifier PASS, implementation checks exit 0, review/findings/closure hashes match |
| `published` | state-bound publication intent; exact previewed and human-authorized CAS push or acknowledged adoption; exact remote postcondition; reviewed endpoint policy; rendered provenance for GitOps; immutable container digest for mixed delivery; when present, an exact finite source -> GitLab render -> protected render branch -> named Argo auto-sync delivery effect |
| `plan-reviewed` | append-only operations-adversary history exists and latest release/plan snapshot review is PASS |
| `apply-running` | reviewed operations plan remains hash-bound before authorization |
| `applied` | every manual target has an exact `attempt-preview`, target/scope-bound human authorization, durable apply intent, and matching applied attempt; every automatic target has a publication-effect-bound, non-mutating converged Argo adoption observation |
| `operationally-verified` | every target is freshly verified through its typed profile against desired identity and all required probes, or has a live human waiver |
| `complete` | current artifacts and freshness still match all bound receipts; later staleness is visible and uses the governed verification-refresh edge |

`complete` is never reached from a free-text external-write ledger. Push,
render, apply, and verification are distinct facts.

The live contract remains typed and narrow. `gitops-manual-sync` performs one
exact revision-pinned Argo sync. `gitops-auto-sync-observe-v1` performs no
mutation and exists only for an exact Argo target already included in the
human-authorized publication delivery effect. Verification profiles are
separate: `argocd-web-workload-v1` proves the public Ingress graph;
`argocd-istio-internal-http-v1` proves an exact same-cluster Service FQDN from a
bound sidecar caller; and `argocd-istio-eastwest-v1` proves the complete sender
to receiver `.global` Istio route. The state graph is unchanged: an automatic
target reaches `applied` only after observed controller convergence, then uses
the same `verify-running` and freshness gates. A generic command, generic
auto-sync label, or named probe string cannot advance state.

Internal `.svc.cluster.local` and `.global` routing identities do not redefine
public application hosts, which continue to use
`{app}.{tenantpostfix}.{environment}.example.com`. Kargo, Crossplane, Keycloak,
control-plane workloads, Pulumi, provider APIs, direct `kubectl` mutations, and
source-backed operational wrappers remain fail-closed.

Operational contexts are part of each target scope. The writer hash-binds and
rechecks JSON-form kubeconfig and Argo config files, their selected
server/context, and embedded CA. Argo argv must include the exact `--config`,
`--argocd-context`, and `--server`; its whole config hash binds the selected auth
token without persisting that credential in evidence. Publication scope likewise
binds the state-owned isolated `HOME` and configless bare push repository.

## Migration

Preview is read-only:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-migrate.py" "$ID" --repo-root "$REPO_ROOT"
```

Apply is backup-first:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-migrate.py" "$ID" \
  --repo-root "$REPO_ROOT" --apply
```

V1 `complete`, `rectify-running`, and `critique-complete` map to
`critique-running`. This deliberately re-runs the v2 adversarial assessment,
rectification, independent closure, and all later gates. Migration always sets
publication and operations to required/pending and quarantines legacy external
write strings as metadata; it never promotes a source state, infers a remote
publication, or infers a live rollout.
