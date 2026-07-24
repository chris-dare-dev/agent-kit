---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---

# Phase 4 — rectify, close independently, and prove code-complete

Rectification is not terminal delivery. It ends at `code-complete`; publication,
apply, and operational verification are later state/evidence gates.

## Main-session rectification

The main session rectifies by default. `milestone-rectifier` is an exception
when explicitly delegated. The implementer cannot certify its own design.

1. Read every file in `state.critique_files` fully.
2. Re-open each C/H citation against the live final branch before changing it.
3. Fix confirmed C/H findings and add the proposed regression guard.
4. Mark invalidated findings with reproducible evidence; never silently drop.
5. Fix small M findings when in scope; defer the rest with reasons. L remains
   deferrable.
6. Re-run exploratory validation, then execute each authoritative command with
   the locked detached-worktree `check-run` writer. It records executable and
   tracked-script hashes plus every failed/successful attempt; never author
   check receipts by hand.
7. Commit locally. Stop before push/publication.
8. Write every disposition through `milestone-pipeline-findings.py set`, then
   run its C/H gate.

Exactly one state field is set: `rectification_commit`, or
`rectification_not_required_reason` for a genuine zero-change closure.

## Independent closure

After rectification, dispatch `milestone-closure-verifier`. It receives explicit
base/final-head, an immutable findings snapshot, assessment projection, active
passing checks, and the complete check-attempt ledger.
It does not fix anything. Implementation evidence is assembled afterward so it
can bind the completed closure report without a circular hash dependency.

The verifier reopens all C/H code, confirms fixed commits and regression guards,
reproduces invalidations, re-hashes review inputs, reruns safe local checks, and
looks for defects introduced by rectification. Missing/unavailable evidence is
`FAIL`, not a waiver.

Append its body/prompt/report/snapshot receipt only with
`milestone-pipeline-artifacts.py review-append --stage closure`. The previous
assessment and closure attempts are immutable. A FAIL remains visible; after
new rectification a distinct task may append another attempt. Only the latest
`verdict=PASS` can satisfy `code-complete`.

Dispatch the closure prompt's **exact persisted bytes** — the validator hashes
the file, not what you sent, so this link is yours alone to hold (artifacts-v2,
"The orchestrator MUST dispatch the persisted prompt's exact bytes").

**If rectification moves HEAD, update `rectification_commit` first.** `check-run`
refuses while `HEAD != state.rectification_commit` ("repository HEAD is not the
final implementation commit"), and the closure verifier binds `FINAL_HEAD` from
it. Set it with the locked writer — `milestone-pipeline-checkpoint.py <id> --set
rectification_commit='"<sha>"'` (legal in `rectify-running`) — then re-run every
check; the ledger is append-only, so the superseded attempts stay visible.

**A non-compliant ASSESSMENT lane cannot be repaired from here.** Rectification
and closure are the only lanes still open at this phase; the assessment binding
is immutable and the phase machine is forward-only. Do not try to rebuild the
manifest or retake `critique-complete` — re-init the milestone instead. Full
rationale and the four refusal points: `milestone-pipeline-artifacts-v2.md`,
"A compromised assessment has NO in-machine repair".

## Implementation evidence

Create `implementation-evidence.json` per
`milestone-pipeline-artifacts-v2.md`. It binds:

- all repo bases/heads/ranges/commits/branches;
- every passing project check and real output reference;
- current review-manifest and findings-register file hashes;
- zero open C/H and findings-gate exit 0;
- rectification claim and closure report hash;
- generated/render test evidence.

The checkpoint calls the same artifact validator while holding the state lock,
re-hashes its receipts, persists bindings, and advances to `code-complete`.
Later replacement of a bound artifact invalidates downstream gates.

## External-write boundary

Do not push, create/merge an MR, sync ArgoCD, run kubectl/Pulumi/provider
mutations, or write AWS/cluster state during rectification. `code-complete`
means the implementation is reviewed and validated locally. The command's
publication/apply phases ask for each concrete external action separately.

There is no v2 `external_writes_authorized` boolean or free-text completion
ledger. Publication evidence belongs in `release-manifest.json`; target-scoped
human authorization and attempts belong in `operations-evidence.json` bound to
the frozen operations plan.
