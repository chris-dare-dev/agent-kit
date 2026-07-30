---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---

# Milestone pipeline v2 artifact contract

The JSON Schemas under `data/schemas/milestone-*-v2.schema.json` define strict
shape and reject unknown fields. `milestone-pipeline-artifacts.py` is the
semantic authority: it binds identities and hashes, derives statuses, evaluates
freshness, verifies append-only history, and returns checkpoint receipts.

Every artifact has this envelope. A deterministic writer-owned artifact uses:

```json
{
  "schema_version": 2,
  "milestone_id": "ISSUE-1234",
  "generation": 1,
  "created_at": "2026-07-12T12:00:00Z",
  "producer": {
    "kind": "deterministic-tool",
    "name": "milestone-pipeline-artifacts.py",
    "provider": "local",
    "version": "<sha256-of-frozen-writer>"
  }
}
```

Artifact `milestone_id` must equal both `state.id` and the state directory name.
Generation starts at 1. Plan changes increment generation, change the canonical
plan hash, and invalidate all prior authorization/evidence/waivers.
Agent-authored review/implementation/release/plan artifacts identify their
actual producer instead; publication intent, operations evidence, and waivers
require the exact deterministic writer identity above.

`state.agent_kit_commit` freezes the writer semantics used by a run. A newer
checkout cannot silently validate or append under that state. `kit-upgrade-preview`
returns the exact old/new commit and writer hashes; `kit-upgrade` requires a
human name plus that exact scope hash and appends `kit_upgrade_history`. The
writer preflights every already-bound artifact under the new semantics before
atomically changing state. Old immutable receipts remain attributable to the
explicitly recorded kit lineage; new receipts use the current kit.

## Review manifest

`review-manifest.json` records the exact base/head and binary diff hash. It must
contain one receipt for every deterministically selected reviewer:

- always: `milestone-adversary` and
  `milestone-delivery-integrity-adversary`;
- frontend paths: `milestone-frontend-ux`;
- infra paths or high-risk infra repo identity: `milestone-infra-safety`.

Each receipt binds role, stage, provider/model, a required distinct runtime task id,
declared canonical agent-body source, a task-specific per-attempt body snapshot path/hash,
persisted dispatch prompt path/hash, critique
path/hash, reviewed base/head, times, and verdict. Codex task names are not role
proof: when the client cannot select a named custom agent, the orchestrator
loads the canonical body into the prompt and the body hash binds that fallback.
This is tamper-evident provenance, not a runtime-signed attestation: the current
Codex collaboration API exposes no receipt-verification endpoint, so the
validator cannot cryptographically prove execution. The orchestrator records
the actual spawned task ids; missing or duplicate ids fail closed.
For the current Codex API this field is the exact path-safe final component of
the canonical task name returned by `spawn_agent`; the orchestrator must choose
that unique leaf before dispatch and verify the returned name, never sanitize
or synthesize it afterward.

Dispatch prompts use one exact envelope followed by the complete body snapshot
and no trailing instructions:

```text
MILESTONE_REVIEW_DISPATCH_V2
ROLE: <role>
STAGE: <assessment|closure|operations>
ID: <id>
REPO_ROOT: <absolute canonical target repo>
WORKSPACE_ROOT: <absolute canonical installed workspace>
<stage-specific uppercase hash-bound inputs>
AGENT_KIT_COMMIT: <full commit>
SOURCE_REMOTE_URL: <canonical origin>
--- CANONICAL AGENT BODY ---
<exact artifacts/reviews/<role>-<task-id>-agent.md bytes>
```

The validator requires the canonical prompt/body paths and exact header map;
substring presence is not accepted.

**The orchestrator MUST dispatch the persisted prompt's exact bytes.** The
validator hashes the prompt FILE; it cannot see what was actually sent (the
no-runtime-attestation note above), so a run that persists a compliant prompt and
then dispatches something else — a short "the body is loaded by the harness"
reference note, say — PASSES validation while producing void provenance, and that
void is unrepairable (next section). This is the one link in the chain no
deterministic check can hold; it is carried by the orchestrator alone.

> **Amended 2026-07-17 — narrowed, NOT repealed.** "No deterministic check can hold
> it" remains true for a **hand-composed** dispatch (a human or model typing the
> prompt into a client): nothing can compare what was sent against the file. It is
> **no longer true for the Workflow-tool path.** `milestone-pipeline-review-prepare.py`
> emits the persisted bytes' UTF-16 length + FNV-1a checksum alongside the prompt,
> and `milestone-pipeline-workflow.mjs` recomputes both over the string it received
> and **throws before dispatching** on any mismatch — so the model-composed `args`
> hop, which is where this failure actually lives on that path, now fails LOUDLY.
> Measured in the live sandbox 2026-07-17: tail-truncation past the body delimiter,
> mid-surrogate truncation, same-length substitution, CRLF→LF, and an appended
> instruction all throw with zero `agent()` calls.
>
> Read the scope precisely, because overclaiming here is the exact defect: the hop is
> **tripwire-covered, NOT closed by construction.** A harness that mutated the string
> between the tripwire and the model call would still be invisible, and nothing
> retains the bytes actually sent. What changed is that the dna-rem-m6 failure —
> compliant file persisted, reference note dispatched, validator green, provenance
> void — cannot pass silently on the Workflow path. **On any other path this
> paragraph stands unamended: send the file.**

Yes, a
client that selects the named agent also loads the body through its own
mechanism, so the body arrives twice; that duplication is the price of one
contract serving clients that cannot select a named agent. Send the file.
Observed 2026-07-15 (dna-rem-m6): prompts were built correctly and dispatched as
reference notes; the validator passed them, and the whole assessment lane's
provenance was void and unrecoverable.

Assessment receipts are blind and immutable. `closure_reviews[]` and
`operations_reviews[]` are locked append-only attempt histories written only by
`review-append`. Each attempt has unique prompt/body/report/input snapshots;
FAIL cannot be erased, and only the latest attempt may bind current inputs and
PASS its phase gate. Historical closure attempts retain their reviewed head,
findings snapshot, and check-ledger prefix; historical operations attempts
retain release/plan snapshots.

### A compromised assessment has NO in-machine repair

**The recovery is a fresh run. There is deliberately no re-bind.** Once
`critique-complete` binds the manifest, four independent mechanisms refuse a
rebuild, and they are the guarantee rather than a gap to route around:

- `artifact_bindings.review_manifest.immutable_root_hash` pins the *assessment*
  projection — rebuilding those receipts fails `review_manifest: immutable
  artifact envelope/root changed`. (The manifest file itself stays mutable, but
  only so `review-append` can add closure/operations attempts.)
- `PHASE_EDGES` is forward-only: `rectify-running` exits ONLY to `code-complete`,
  so the `critique-complete` binding cannot be retaken.
- `review-append` requires `findings_register.critique_files` to equal the
  hash-bound review set. Extracting an out-of-band review's findings (an external
  audit) into the register therefore blocks closure for that run — the register
  may only cite critiques this manifest binds. Such findings belong to the run
  that binds them.
- `recover` only replays interrupted transactions; `waiver-append` is keyed to
  operations plans, not reviews.

So do NOT prescribe "rebuild `review-manifest.json`, then re-run
`critique-complete`" in a handoff — the tooling refuses it from every direction
and the next session will burn hours discovering that. If an assessment lane is
found non-compliant after `critique-complete`, **re-init the milestone**; the
code, the critique files, and the findings register carry over as inputs to the
fresh run. Deliberately NOT offered: a human-approved assessment re-bind. The
blind assessment is the one lane whose value is that it cannot be revisited after
the fact; an approval-gated escape hatch is exactly what gets used under deadline
pressure. Observed 2026-07-15 (dna-rem-m6): the closure lane correctly FAILED on
the resulting open finding, and the milestone could not close in that run.

## Implementation evidence

`implementation-evidence.json` contains:

- `repositories[]`: exactly one source-repo record in v2.0, with
  base/head/range, complete ordered commits, branch, and path. Split multi-repo
  work into dependency-linked milestones; multi-target operations remain
  supported;
- `checks[]`: exact detached-worktree command, exit code, executable/script
  hashes, setup receipt, completion time, and a real evidence reference;
- `critique`: hashes of the current review manifest and findings register,
  findings-gate exit code, and derived open C/H counts;
- `rectification`: either a commit or a non-required reason, plus the closure
  report-file hash (the review manifest separately binds the full receipt);
- generated/render test evidence.

An evidence reference is not a dangling checksum. It includes path, SHA-256,
media type, byte size, collector, and optional command.

The locked `check-run` writer executes committed source in a fresh detached Git
worktree. It rejects untracked primary-worktree inputs and ambient external
scripts, hashes trusted executables and tracked script blobs, and appends every
failed/timed-out/successful receipt to `state.check_run_attempts`. Active green
checks are a separate projection; closure receives both. Node package-manager
checks install in the disposable tree from the tracked lockfile with frozen
resolution and lifecycle scripts disabled. A project needing richer setup must
use a reviewed tracked wrapper. No ignored dependency cache is mounted.

At the code gate, repository markers select mandatory exact commands: declared
Node build/test scripts require the matching package-manager build/test
commands; Go requires `go test ./...`; Rust requires `cargo test`; and Python
test projects require `python3 -m pytest`. Additional checks are welcome, but a
generic `true`/`exit 0` receipt is refused. A repository with no recognized
marker must add a reviewed `.milestone-pipeline/checks.json` exact-command
contract.

## Publication intent

`publication-intent.json` is a deterministic, state-bound authorization record;
it is not agent-authored evidence. `publication-preview` performs read-only
remote discovery and returns the exact remote, branch, reviewed commit, observed
remote head, compare-and-swap push argv (or adoption), isolated Git environment,
executable/known-host identities, scope hash, and any superseded scope.

After a human approves that exact preview, `publication-authorize` re-observes
the scope and persists the authorization before mutation. `publication-apply`
executes only the persisted compare-and-swap action, records an append-only
execution attempt, and verifies the exact remote postcondition. The alternative
`adopt-preexisting` mode requires the same explicit preview/acknowledgement and
proves the reviewed commit was already present without pushing. Ambiguous
observed success is receipted and never speculatively replayed; a new intent may
supersede an old one only through the writer.

Git executes from a state-owned isolated `HOME` using a recreated configless
bare push repository and only the reviewed repository's object directory.
System/global config, hooks, and prompts are disabled and SSH uses an explicit
hash-bound known-hosts file. Userinfo URLs, local URL rewrites, includes, custom
hooks, credential helpers, and ambient SSH config are rejected.

## Release manifest

`release-manifest.json` separates:

- source revision;
- published remote branch revision, which must equal the exact reviewed commit;
- rendered GitOps revision;
- immutable deployable artifact digest.

`delivery_kind` is `source-only`, `gitops`, `mixed`, or
`not-required`. GitOps/mixed requires a rendered revision. Mixed delivery
requires a resolved immutable container digest (v2.0 has no generic artifact
kind). A local commit or push command string is
not publication evidence. At the publication gate, the validator requires the
source set to exactly equal the reviewed implementation head and re-runs
`git ls-remote`; descendant or merge commits are refused as unreviewed. GitOps rendered refs are also
re-read from their declared remote/branch.

Production publication also loads `.milestone-pipeline/trust-policy.json` from
the exact reviewed source commit. It must bind the exact source remote,
rendered-repository prefixes, registry prefixes, and an absolute artifact
resolver path plus SHA-256. Caller environment/PATH overrides are not trusted.
The resolver must be a system/package-manager `crane` binary and is invoked in
an isolated environment only as `crane digest <digest-qualified-uri>`; arbitrary
hashed wrappers are not accepted.
Every GitOps/mixed rendered commit must carry a machine-owned source-revision
provenance blob (`{source_repo, source_commit, target_ids, artifacts}`) at the
per-revision `provenance_path` — the framework `milestone-render-provenance.py`
and the platform's `ci-cd-templates/scripts/helm/gitops-provenance.py`
(`.workspace/source-revisions/<app>.json`) emit the same claim schema. Existing
rendered repos without such a producer fail closed until their CI adopts it. A
rendered revision binds to **either** a declared `source_revisions[]` entry
(renderer records the reviewed source directly) **or** a declared
`intermediate_revisions[]` chart-bump hop (the platform records the *chart*
repo+commit as the render source).

**`intermediate_revisions[]` (optional — Capability C).** A source milestone whose
render is triggered by an image-tag bump in a *different* repo (the chart) declares
that hop here: `{repo, remote, branch, commit, role: "chart-bump", binds_image_tag,
verified_at, evidence}`. `binds_image_tag` must be a short-sha prefix of a declared
source commit — this threads the Go source through the chart bump to the deploy
render. Omitting it keeps the pre-existing behavior; `source-only`/`not-required`
carry none.

**Tag-bound artifacts (Capability B1).** The platform renderer records `artifacts:
[]` (the image is pinned by a mutable tag, not a digest). Every provenance-bound
artifact must still be released, but a released artifact absent from provenance is
admissible only when a declared `intermediate_revisions[]` `binds_image_tag`
live-resolves through the trust-policy `crane` to that immutable digest
(`crane digest <registry_repo>:<tag>` == the released `sha256:` digest). This binds
the image digest at release time without a renderer-CI change; recording the digest
in provenance at render time (the durable form) makes the release-time resolution
unnecessary.

Trust-policy schema v1 remains valid for the manual delivery path. Schema v2
adds `automatic_gitops` and accepts exactly
`ci-render-argocd-auto-sync-v1`: one source-publication step, one hash-bound
GitLab CI render step, one protected render-publication step, then exactly one
named Argo auto-sync step per finite target. Each target binds its Application
resource, Argo endpoint/context/config/CA, project, render source/path/ref,
destination, explicitly enabled automated/prune/self-heal/allow-empty policy,
and the SHA-256 of
the complete later verification action surface. Publication preview reifies
this declaration as `scope.delivery_effect`, resolves the live Application UID,
and rechecks every identity before mutation. Unknown/generic auto-sync, an
omitted material edge, an extra target, or a changed renderer is not
authorized. `adopt-preexisting` always has a null delivery effect because a
current acknowledgement cannot authorize past controller writes.

Schema v2 also accepts `ci-render-argocd-auto-sync-fanout-v1` for the platform
**source → image → chart-bump → N-deploy-repo fan-out**. It replaces the singular
`render`/`ci_render` with: an `image_build` hop (binds the reviewed ECR
`registry_repo` and `tag_scheme: source-short-sha`); an intermediate `chart` hop
(the repo the source CI bumps to trigger the render); `render_legs[]` (one protected
deploy repo per leg, each with its own `ci_render`); and `targets[]` where each
target's `render_leg_id` resolves to a leg and its Argo source repo/revision must
equal *that leg's* remote/branch. A target addresses its cluster by
**`destination_server` (HTTPS URL) XOR `destination_name` (a registered-cluster name)** —
the platform's commercial ApplicationSets use `destination.name`, so the effect reification
compares whichever field the target declared against the live `spec.destination`. (The
operations-phase verification profiles still assume `destination_server`; a name-addressed
cluster's *operations* plan is a separate follow-on.) The `cascade_steps` DAG is exact and ordered:
`source-publication → image-build → chart-bump → per-leg (ci-render →
render-publication) → one argocd-auto-sync per target (depending on its leg's
render-publication)`. Because the render is triggered by the chart-bump on the
chart branch (not the source push), each leg's `ci_render.source_ref` binds the
chart branch. Preview reifies each leg's live render head and every target's live
Application UID; `adopt-preexisting` remains null-effect. Single-leg and fanout are
discriminated by `kind`; both stay valid.

**Cascade lag.** The release manifest binds publication-time facts (rendered
commits + resolved artifacts); *live delivery* (Synced/Healthy at the desired
revision, pod image digest) is proven in the operations phase, not at `published`.
A GitOps milestone therefore emits `gitops`/`mixed` only once the cascade has
landed for its source (the live deploy render reflects this source's image), and
sets `operations_required: true`; otherwise it stays `source-only` and defers the
live-delivery proof to operations.

```json
{
  "schema_version": 1,
  "source_remote": "git@git.example.com:group/source.git",
  "render_remote_prefixes": ["git@git.example.com:deploy/"],
  "artifact_registry_prefixes": ["registry.example/workspace/"],
  "artifact_resolver": {
    "path": "/opt/homebrew/bin/crane",
    "sha256": "<sha256-of-reviewed-executable>"
  }
}
```

Use `artifact_resolver: null` only when the release cannot contain artifacts.

## Frozen operations plan

`operations-plan.json` owns one record per environment/account/cluster/resource
target. It binds desired source/render/digest/generation, immutable execution
contexts and trust roots, apply method, operations and verification owners,
mandatory verification contract, rollback, and a plan-wide maximum evidence
age. Mutation/observation/probe commands are exact argv arrays with absolute
SHA-256-pinned executables and bounded timeouts.

V2 accepts two delivery methods:

- `gitops-manual-sync`: exact remote `argocd app sync` with the desired rendered
  revision and frozen Argo server;
- `gitops-auto-sync-observe-v1`: no apply command. It is legal only when the
  plan target set and each `auto_sync_binding` exactly match the authorized
  publication effect and released source/render identities. Its action hash
  covers execution environment/contexts, typed profile, observation, and
  verification contract before publication; release/plan cross-links later
  bind the exact renderer-created commit.

V2 has three separate verification profiles:

- `argocd-web-workload-v1`: a typed identity/behavior graph binding one Argo
  Application to its exact source/project/destination, one Deployment and UID,
  selected Pods/container, one Service and UID/port/selector, one Kubernetes
  Ingress and UID/exact route, and one credential-free HTTPS URL with an exact
  expected 2xx status.
- `argocd-istio-internal-http-v1`: an exact same-cluster
  `<service>.<namespace>.svc.cluster.local` Service FQDN, named port/targetPort,
  ready EndpointSlices, reviewed sidecar caller UID/SA/image, and bounded HTTP
  2xx service smoke.
- `argocd-istio-eastwest-v1`: an exact tenant-cluster
  `<service>.<namespace>.svc.cluster-<tenant>.global` route, sender and receiver
  ServiceEntry/DestinationRule identities, receiver EnvoyFilter and gateway
  proxy identity, split sender/receiver xDS and endpoint proof, and bounded
  sender-side HTTP 2xx smoke.

The east-west context topology is itself bound: sender and receiver Kubernetes
API servers must be distinct, while the receiver mesh context and the workload
context must name the same server and CA. Istio proxy queries use qualified
`pod.namespace` identities rather than ambiguous pod names.

Collectors parse full JSON/status output themselves. A probe name cannot be
used to relabel an unrelated command or literal template output. Each profile
has an exact, non-interchangeable probe set; all include Argo, Deployment, and
Service identity, and add `pod-image-digest` whenever a desired image digest
exists. Internal profiles cannot borrow Ingress evidence. A bounded
`kubectl exec` smoke is an active verification action, so an automatic
publication effect must bind its exact executable/context/argv/timeout surface
before it can run.

The profile requires hash-bound, non-symlinked JSON context files and rechecks
them immediately before execution. Kubernetes commands must use the exact
`--kubeconfig` and `--context`; the selected JSON context binds the HTTPS cluster
server and embedded CA and forbids exec/auth-provider/token-file plugins. Argo
commands must use the exact `--config`, `--argocd-context`, and `--server`; the
selected JSON context binds its server, user, embedded CA, and the whole config
file hash, including the selected auth token. Tokens and all other credential
bytes are never copied into persisted evidence; receipts keep only projected
identity/verification facts and hashes.

The internal `.svc.cluster.local` and `.global` names are mesh routing
identities, not public hostnames; public application ingress continues to use
`{app}.{tenantpostfix}.{environment}.example.com`. Kargo, Crossplane, Keycloak,
control-plane workloads, Pulumi, provider APIs, direct `kubectl` mutations, and
source-backed operational wrappers remain unsupported. They cannot borrow a
typed profile's completion claim.

`plan_hash` is SHA-256 of canonical JSON excluding the `plan_hash` field.
Target `scope_hash` is SHA-256 of `plan_hash + newline + canonical target JSON`.
The helper prints both:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" plan-hash operations-plan.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" scope-hash operations-plan.json target-id
```

Plans with required operations need at least one target. `not_required` needs a
nonempty rationale and an empty target set. Target IDs and probe kinds must not
collide after evidence-path normalization.

## Append-only operations evidence

`operations-evidence.json` has the exact frozen target set. Each target owns
`attempts[]`; attempt ids are globally unique, sequence is contiguous, and each
attempt hashes its predecessor. An attempt contains either:

- target/scope-bound `human-explicit` authorization plus a manual apply receipt;
  or
- target/effect-bound `publication-effect` authorization plus
  `observed-auto-sync-v1`, its exact Argo observation receipt, and no apply
  command/idempotency/intent receipt;
- apply/adoption status, observed desired identity, actor/time, and evidence;
- verification status, observed desired identity, and per-probe evidence.

Target status is derived from the latest attempt; stored status must match.
Verification timestamps may not be future-dated or older than the plan's
maximum age when that attempt is the current delivery claim. Historical
attempts remain hash-valid after their observations age out. Writer-provided
`fresh_until` is deliberately absent.

Operation attempts are written only by the locked `attempt-start`,
`attempt-apply`, `attempt-adopt-auto-sync`, `attempt-verify`, and
`attempt-verify-recover` subcommands.
`attempt-preview` first renders the exact target action and scope hash;
`attempt-start` materializes target/scope authorization only after explicit
human approval. Later commands may extend only the latest attempt.
`attempt-apply` executes the exact plan-bound,
SHA-256-bound apply argv, then executes the frozen observation argv and derives
success only when observed identity equals desired identity. The caller cannot
submit status, exit code, observed JSON, or a substitute evidence file.
For an automatic target, `attempt-preview` returns a non-mutating observation
and no approval request; `attempt-start`/`attempt-apply` refuse it.
`attempt-adopt-auto-sync` revalidates the publication intent/effect and plan
binding, runs only the exact Argo Application GET, and appends a terminal
converged or failed observation. It never invokes a sync or replays a controller
write.
Every mutable review/operation/waiver write uses a prepared transaction journal;
recovery revalidates the artifact and reconstructs the only permitted state
binding after a crash before completing or discarding the transaction.

Initial verification of a pending applied attempt introduces no new delivery
mutation and does not need another authorization. Any bounded active smoke was
already part of the exact target scope (and the publication action hash for an
automatic target). Re-verification is different: `attempt-preview
--attempt-id` returns a hash of the exact live observation scope, and
`attempt-verify --approved-by --scope-hash` commits a refresh intent before any
collector executes. A crash after that boundary leaves an unresolved intent.
`attempt-verify-recover` closes it as `ambiguous` without replaying any command;
a later refresh needs a new preview and authorization.

Receipts persist projected non-secret identity and typed verification facts,
not raw operational stdout/stderr. ArgoCD sync is one fact. It cannot substitute
for observed resource generation, Pod digest, Service/Ingress connectivity, or
behavioral smoke. A multi-target partial rollout remains partial and cannot
advance the top-level gate.

## Waivers

`waivers.json` is plan- and target-scope bound. A waiver needs the exact missing
contract, human approval, creation/approval/expiry times, reason, compensating
control, and a different follow-up milestone. Future, nonhuman, cross-target,
whole-contract, or stale-plan waivers fail. Expired entries remain append-only
history but are inactive and cannot satisfy a gate. New waivers are written
only through `waiver-append`, must match current verification gaps exactly, and
expire within 30 days.

## CLI and exit behavior

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" validate <kind> <path> --state state.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" gate --state state.json --phase code-complete
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade-preview --state state.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade --state state.json --approved-by HUMAN --scope-hash SHA256
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" check-run --state state.json --name NAME -- COMMAND ARG...
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append --state state.json --stage closure --receipt RECEIPT.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append --state state.json --stage operations --receipt RECEIPT.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-preview --state state.json --mode publish
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-authorize --state state.json --mode publish --approved-by HUMAN --scope-hash SHA256
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-apply --state state.json
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-preview --state state.json --target TARGET [--attempt-id ATTEMPT]
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-start --state state.json --target TARGET --approved-by HUMAN --scope-hash SHA256
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-apply --state state.json --target TARGET --attempt-id ATTEMPT --actor ACTOR --collector COLLECTOR
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-adopt-auto-sync --state state.json --target TARGET --collector COLLECTOR
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify --state state.json --target TARGET --attempt-id ATTEMPT [--approved-by HUMAN --scope-hash SHA256]
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify-recover --state state.json --target TARGET --refresh-id REFRESH
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" waiver-append --state state.json --target TARGET ...
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" reconcile --state state.json
```

Exit 0 means the claim validates. Exit 3 means REFUSED and includes every
deterministic reason. Reconciliation is blocking; never append `|| true`.
`complete` records a fresh verified snapshot but status continues live
freshness evaluation; required stale evidence re-enters the governed
`verify-running` refresh edge. It reaches `apply-running` only when a separately
previewed and authorized mutation is actually needed.
