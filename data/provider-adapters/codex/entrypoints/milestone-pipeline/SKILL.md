---
name: milestone-pipeline
description: Run or resume the delivery-state v2 milestone pipeline in Codex when a nontrivial milestone needs separate code-complete, published, applied, and operationally-verified claims; parallel blind research; mandatory independent adversarial reviews; closure verification; and exact human-authorized external-write gates. Invoke as $milestone-pipeline ID --repo-root ABSOLUTE_REPO_PATH with optional --deep or --resume.
---

# Codex milestone pipeline adapter

This is a thin Codex orchestration adapter over the canonical pipeline command
and shared deterministic scripts. It does not reimplement state or delivery
semantics.

## Invocation and discovery

Use:

```text
$milestone-pipeline <ID> --repo-root "/absolute/path/to/target/repo" [--deep] [--resume]
```

Run Codex from the workspace root (default
`$HOME/Work/workspace`). Codex discovery stops at nested git roots, so starting in a
target repo may hide the workspace skill/agent pack. Every git command must use
`git -C "$REPO_ROOT"`; every pipeline script uses
`$WS/.claude/scripts/...`. Do not rely on the process CWD.

Set `AGENT_KIT_ROOT` to
`$WS/GitLab/workspace/platform/tools/claude-mcp-server`. Read these
canonical contracts completely before acting (references may be loaded by their
MCP names when available):

- `.claude/commands/milestone-pipeline.md`
- `.claude/references/milestone-pipeline-state-schema.md`
- `.claude/references/milestone-pipeline-artifacts-v2.md`
- `.claude/references/milestone-pipeline-agent-contract.md`
- `.claude/references/pipeline-pattern-v2.md`

If `state.json` is v1/unversioned, stop normal execution and run the migration
preview. Never silently reinterpret legacy `complete` as live operation.
Legacy terminal/post-critique states re-enter `critique-running`, not closure or
operations. Apply migration only after presenting that downgrade.

The state freezes its pipeline-kit commit. If the installed writer has advanced,
run `kit-upgrade-preview`, show its exact old/new commit and writer hashes, and
run `kit-upgrade --approved-by ... --scope-hash ...` only after explicit human
approval. Never make old receipts silently inherit new semantics.

## Codex collaboration mapping

The main Codex agent is the sole orchestrator and state writer. Subagents never
spawn subagents and never advance phases.

- Claude `Agent` fan-out maps to Codex collaboration agents spawned concurrently.
- The current collaboration API may not expose a named custom-agent selector.
  A task name is not role proof. Before every dispatch, load the canonical
  `data/agents/<role>.md` body, persist a task-specific per-attempt body snapshot, include that
  snapshot in the task prompt, persist the exact prompt below the milestone
  artifact directory, and later record all hashes plus the actual non-null
  spawned task id in `review-manifest.json`. In the current collaboration API,
  `spawn_agent` returns a canonical task name such as `/root/<safe-leaf>` rather
  than a separate opaque id. Choose a globally unique leaf matching
  `[A-Za-z0-9][A-Za-z0-9._-]*` before dispatch, verify the returned canonical
  name ends in exactly that leaf, and record that exact returned leaf as
  `agent_task_id`; do not sanitize, hash, or otherwise invent an identifier. The
  current harness has no signed receipt-verification endpoint, so this is
  prompt-enforced, tamper-evident provenance rather than a signed runtime
  identity or cryptographic execution proof. Stop if the
  runtime returns neither an opaque id nor the exact canonical task name/leaf
  pair.
  Use the exact `MILESTONE_REVIEW_DISPATCH_V2` header/body envelope defined in
  `milestone-pipeline-artifacts-v2.md`; no free-form suffix may follow the body.
- Use minimal/no inherited conversation for blind adversarial reviews. Supply
  only the canonical role body and explicit ID/repo/range/output inputs. Shared
  filesystem access means cryptographic blindness is unavailable; reviewers are
  nevertheless forbidden to read sibling critiques before their own output is
  complete.
- The generated Codex agent pack does not turn Claude `tools:` frontmatter into
  a runtime sandbox. Read-only reviewer scope is prompt-enforced. Record target
  HEAD/status before dispatch and verify them again after every review wave;
  unexpected tracked-tree or ref mutation invalidates the wave and stops.
- Wait for every required agent. A missing role, malformed return, timeout, or
  unavailable role body leaves the current phase running and stops the pipeline.
- Use worktrees only for parallel tracked-file mutation. Review/research agents
  operate in the shared target repo read-only and write only their assigned
  untracked artifact/critique output.

## Required review topology

Every nontrivial implementation gets two blind, parallel assessment lanes:

1. `milestone-adversary` — correctness, security, platform conventions, and
   integration.
2. `milestone-delivery-integrity-adversary` — schema, state-machine, replay,
   evidence, publication, authorization, operations, freshness, and concurrency.

Add `milestone-frontend-ux` and/or `milestone-infra-safety` when the canonical
diff/identity rules select them. The artifact validator independently computes
that set; the orchestrator cannot omit a selected reviewer by editing the
manifest.

Use capacity-aware waves when all four roles are required: keep the two
always-on adversaries together in wave one, fill remaining child capacity with
a conditional reviewer, then run the final conditional reviewer without
including or summarizing sibling outputs. Shared-filesystem blindness remains
a prompt-enforced policy, not a cryptographic isolation guarantee.

After rectification, run authoritative checks only through `check-run`, then
dispatch `milestone-closure-verifier` against the final head, findings snapshot,
active green checks, and the complete failed/successful attempt ledger. It
cannot implement or rectify. Append each closure FAIL/PASS through
`review-append`; old attempts and their task-specific prompt/body/snapshot files
remain immutable. `code-complete` requires the latest hash-bound `PASS` plus a
current findings gate and passing implementation evidence.

Do not expose sibling critiques to a reviewer before all assessment lanes have
returned. Only then extract every critique into the single findings register.

## Delivery lifecycle

Follow the canonical Research -> Implement -> Critique -> Rectify phases, then:

1. Build and validate `review-manifest.json` and
   `implementation-evidence.json`; advance to `code-complete` only through the
   checkpoint tool.
2. Stop at each external-write boundary. Starting this skill is not permission
   to push, sync, apply, mutate a provider, or write cluster state.
3. Before publication, advance to `publish-running` and run
   `publication-preview --mode publish` (or `adopt-preexisting`). Present the
   exact returned scope/action. After the human approves that scope, run
   `publication-authorize` with the same mode/hash and then
   `publication-apply`. Never push directly. A schema-v2 automatic GitOps
   declaration is legal only when this exact publication scope enumerates the
   source -> GitLab CI render -> protected render branch -> every named Argo
   Application cascade edge before the push; generic auto-sync is forbidden.
   Build `release-manifest.json` only
   from the writer's exact remote postcondition; `published` also requires
   rendered revision/artifact digest evidence when applicable.
4. Freeze `operations-plan.json`, advance to `plan-review-running`, and dispatch
   `milestone-operations-adversary` against immutable release/plan snapshots.
   Append all attempts with `review-append`; the latest must PASS before
   `plan-reviewed -> apply-running`.
5. For each manual target, run `attempt-preview`, present its exact action/scope,
   and call `attempt-start --scope-hash ...` only after human approval. Append
   the deterministic apply result to `operations-evidence.json`. For an exact
   `gitops-auto-sync-observe-v1` target, preview requests no second apply
   approval: run `attempt-adopt-auto-sync`, which only observes the publication-
   authorized Argo effect and cannot execute or replay a sync. `applied`
   requires every target's matching manual receipt or automatic convergence
   observation.
6. In `verify-running`, preview the selected attempt and run the typed
   collectors. Initial pending verification introduces no new delivery
   mutation; any bounded active smoke was already in the authorized target
   surface. Re-verification
   requires a new preview, human name, and scope hash; the writer commits the
   refresh intent before commands run. If it is left unresolved, use
   `attempt-verify-recover` to record ambiguity without replay. Fresh desired
   identity and every required typed probe (or an exact valid waiver) are
   required for `operationally-verified`.
7. Run blocking reconciliation and advance to `complete`. Never append
   `|| true`. Status continues to revalidate freshness; stale required evidence
   uses the governed `complete -> verify-running` edge. Re-apply only through a
   separately previewed and authorized mutation when verification shows it is
   needed.

For code/local-only work, set publication and operations to not-required with
explicit rationales before `code-complete`; only then may the short transition
reach `complete`. Published work may skip operations only with the explicit
operations-not-required rationale.

## Machine authority

Use only these writers:

```bash
bash "$WS/.claude/scripts/milestone-pipeline-init-state.sh" "$ID" --repo-root "$REPO_ROOT" --brief "<verbatim ask>"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" <phase>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" validate <kind> <path> --state "$STATE"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade-preview --state "$STATE"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade --state "$STATE" --approved-by <human> --scope-hash <sha256>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" check-run --state "$STATE" --name <name> -- <command> <args...>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append --state "$STATE" --stage closure --receipt <receipt.json>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append --state "$STATE" --stage operations --receipt <receipt.json>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-preview --state "$STATE" --mode <publish-or-adopt-preexisting>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-authorize --state "$STATE" --mode <publish-or-adopt-preexisting> --approved-by <human> --scope-hash <sha256>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-apply --state "$STATE"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-preview --state "$STATE" --target <id> [--attempt-id <id>]
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-start --state "$STATE" --target <id> --approved-by <human> --scope-hash <sha256>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-apply --state "$STATE" --target <id> --attempt-id <id> --actor <operator-or-tool> --collector <collector>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-adopt-auto-sync --state "$STATE" --target <id> --collector <collector>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify --state "$STATE" --target <id> --attempt-id <id> [--approved-by <human> --scope-hash <sha256>]
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify-recover --state "$STATE" --target <id> --refresh-id <id>
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" waiver-append --state "$STATE" --target <id> ...
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" reconcile --state "$STATE"
```

Invoke `publication-authorize`, `attempt-start`, refresh-mode `attempt-verify`,
kit upgrade, and `waiver-append` only after the user explicitly authorizes the
exact previewed scope. Agents author plans and JSON/Markdown
evidence; deterministic tools own check/review/operation-attempt/waiver writes, validation,
derived statuses, bindings, legal transitions, and terminal claims. Never edit
`state.json` directly and never manufacture a receipt for a reviewer that did
not run.

Publication fails closed unless the reviewed source commit contains
`.milestone-pipeline/trust-policy.json`. GitOps/mixed releases additionally
need renderer-produced `.workspace/source-revision.json`; use
`milestone-render-provenance.py` in renderer CI. The reviewed policy pins the
source origin, rendered/registry prefixes, and exact artifact-resolver
executable hash. Artifact resolution accepts only a hash-bound
system/package-manager `crane` binary with its exact `digest URI` invocation.
Do not use wrappers or environment resolver overrides. Generic/inferred
auto-sync is forbidden. Trust-policy schema v2 may declare only the exact
finite `ci-render-argocd-auto-sync-v1` cascade; preview revalidates every live
Application UID/config/CA/source/destination/automated-policy binding and
includes the entire conditional effect in the human authorization. Adoption of
an already-published source cannot retroactively authorize past effects.
The publication writer uses a state-owned isolated `HOME` and a recreated
configless bare push repository; never substitute the source checkout's Git
config, hooks, or credential helpers.

Operational delivery is deliberately narrow. V2 supports exact revision-pinned
`gitops-manual-sync` and the non-mutating, publication-bound
`gitops-auto-sync-observe-v1`. Verification profiles are distinct:

- `argocd-web-workload-v1` proves Argo Application -> Deployment -> selected
  Pods -> Service -> Kubernetes Ingress -> exact credential-free HTTPS 2xx;
- `argocd-istio-internal-http-v1` proves the exact same-cluster
  `<service>.<namespace>.svc.cluster.local` Service, EndpointSlices, reviewed
  sidecar caller, and bounded service smoke; and
- `argocd-istio-eastwest-v1` proves the exact tenant-cluster `.global` route,
  sender/receiver ServiceEntry and DestinationRule state, receiver EnvoyFilter
  and gateway identity, distinct sender/receiver API servers, qualified
  `pod.namespace` xDS/endpoints, and bounded sender-side smoke.

The internal names are routing identities, not public ingress names; public
hosts remain `{app}.{tenantpostfix}.{environment}.example.com`. A bounded
`kubectl exec` smoke is active verification and must be hash-bound in the
preauthorized action surface. Kargo, Crossplane, Keycloak, control-plane
workloads, Pulumi, provider APIs, direct `kubectl` mutations, and source-backed
operational wrappers remain fail-closed. Never relabel an arbitrary command as
a supported probe.

Every supported target hash-binds regular JSON-form Kubernetes and Argo config
files and rechecks them immediately before execution. Commands must carry the
exact `--kubeconfig`/`--context` and
`--config`/`--argocd-context`/`--server` values. The selected contexts bind
their HTTPS endpoints and embedded CAs; the whole Argo config hash binds its
selected auth token. Never persist tokens or other credential bytes in
operational evidence.

## Phase 5 artifact receipt

Only after the authoritative checkpoint reaches `complete`, emit one
append-only receipt for the finalized local evidence set:

```bash
if [ -f "$WS/scripts/artifact_skill_capture.py" ]; then
  python3 "$WS/scripts/artifact_skill_capture.py" emit \
    --workspace "$WS" --producer milestone-pipeline --run-id "$ID" \
    --root "$REPO_ROOT/.claude/notes/milestones/$ID" --apply
fi
```

The receipt is routing intent for a later incremental ingester, not an
ingestion action. It writes to neither Qdrant nor Graphiti and keeps Graphiti
bulk ingestion disabled. Never emit before `complete`; never alter delivery
state, rewrite evidence, or delete prior receipts to repair capture. Report
`created`, `idempotent`, `unavailable`, or `failed` in the final handoff.

## Final handoff

Lead with the delivery claims separately:

- code/closure: `code-complete` status and exact reviewed revision;
- publication: required/not-required/pending/published and exact remote revision;
- apply: per-target pending/failed/applied revision and authorization scope;
- operational verification: per-target verified/waived/pending state and
  evidence freshness.

Then report reviewer roles, findings/closure result, artifacts, checks, and the
ingestion-receipt result, followed by the next external authorization (if any).
Never call the milestone complete merely because code and checklists are
complete.
