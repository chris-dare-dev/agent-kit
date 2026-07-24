---
description: Run the canonical delivery-state v2 milestone pipeline (Research → Implement → independent adversarial review → Rectify/closure → Publish → Apply → Verify). Use for a non-trivial fixed milestone id when code/checklist completion must be distinguished from live operational delivery.
argument-hint: "<id> [--deep] [--resume]"
codex-adapter: skill
type: command
project: milestone-pipeline
status: active
tags:
  - type/command
  - project/milestone-pipeline
  - status/active
---

# Milestone pipeline orchestrator — delivery-state v2

The main session is the sole orchestrator, state writer, findings reconciler,
and owner of every human authorization boundary. Subagents never spawn other
subagents, advance state, push, apply, or self-certify closure.

This pipeline separates the delivery claims:

```text
code-complete -> published -> applied -> operationally-verified -> complete
```

A local commit, green check, push, rendered GitOps revision, applied workload,
and verified behavior are different facts. `plan-reviewed` is a mandatory
control gate, not a synonym for any delivery claim. Never collapse these facts
into one checkbox or a free-text external-write ledger.

Read before running:

- `data/references/pipeline-pattern-v2.md`
- `data/references/runtime-contract.md`
- `data/references/milestone-pipeline-state-schema.md`
- `data/references/milestone-pipeline-artifacts-v2.md`
- `data/references/milestone-pipeline-agent-contract.md`
- `data/references/milestone-pipeline-critique-format.md`

## Arguments and scope

- `ID`: required stable milestone id; everything before the first `--`.
- `--deep`: one deep researcher instead of the standard two-agent fan-out.
- `--resume`: inspect state and resume at its current phase; never replay a
  completed phase.

If no id was supplied, stop and ask for it. Invoke for non-trivial work with a
fixed outcome: a new module/endpoint/chart, a cross-cutting refactor, or a
delivery milestone. Do not invoke for typos, lint-only edits, simple dependency
bumps, reverts, or direct work in `deploy/argocd-config-*` (the initializer
refuses generated deploy repos by identity).

Required CWD for Claude is inside the target git repo. The Codex adapter runs
from the workspace root and passes an absolute `--repo-root`; it uses
`git -C` for target-repo commands. In both cases:

```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -d "$WS" ] || { echo "Set PERSONAL_WORKSPACE_ROOT" >&2; exit 1; }
[ -d "$REPO_ROOT" ] || { echo "Set REPO_ROOT to the target git repo" >&2; exit 1; }
STATE="$REPO_ROOT/.claude/notes/milestones/$ID/state.json"
```

## Step 0 — initialize or migrate explicitly

```bash
bash "$WS/.claude/scripts/milestone-pipeline-init-state.sh" "$ID" \
  --brief "<verbatim user request>" --repo-root "$REPO_ROOT"
```

Fresh init creates schema v2 and performs the roadmap dependency gate. Exit 3
means unmet dependencies; stop unless the user explicitly supplies an audited
override reason. Exit 5 means an existing v1/unversioned state; preview the
one-way migration:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-migrate.py" "$ID" \
  --repo-root "$REPO_ROOT"
```

Do not apply migration without presenting the downgrade: v1 `complete` becomes
`critique-running`, operations become `pending`, and legacy ledger strings are
quarantined as migration metadata. Remote publication is retained only through
the normal v2 evidence gate.

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-migrate.py" "$ID" \
  --repo-root "$REPO_ROOT" --apply
```

On resume:

```bash
bash "$WS/.claude/scripts/milestone-pipeline-status.sh" "$ID" --repo-root "$REPO_ROOT"
```

Jump to the current phase. Never edit `state.json` directly.

The state freezes the exact pipeline-kit commit and deterministic writer
identity. If the executing kit has advanced, normal writers fail closed. Show
the complete upgrade scope first, then proceed only after the human approves
that exact hash:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade-preview \
  --state "$STATE"
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" kit-upgrade \
  --state "$STATE" --approved-by "$HUMAN" --scope-hash "$SCOPE_HASH"
```

Kit upgrade is an explicit state transition with append-only history. Never
silently reinterpret old receipts under new writer semantics.

## Step 1 — research (parallel and independent)

Checkpoint first:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" research-running
```

Dispatch from the main session:

| Mode | Trigger | Lanes |
|---|---|---|
| Standard | default | two `milestone-researcher` agents in parallel |
| Deep | `--deep`, novel architecture, >2 subsystems | one deep researcher |
| Single | explicitly small/quick, ≤200 LOC | one researcher |

Each prompt contains the exact ID, verbatim brief, absolute workspace/repo,
assigned brief path, and mode. Agents write only their distinct brief. Validate
the return against `milestone-pipeline-agent-contract.md`; redispatch once on a
bad shape/artifact, then fail closed.

After every required brief exists:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'research_briefs=["<a>","<b>"]'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'research_mode="standard"'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" research-complete
```

## Step 2 — synthesize and implement

Read every brief. Record the base before the first implementation commit:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" implement-running
BASE=$(git -C "$REPO_ROOT" rev-parse HEAD)
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set "implementation_base=\"$BASE\""
```

Choose one path:

- specialist: one matching workspace specialist;
- inline: main session for ≤500 LOC / ≤5 files with no novel architecture;
- delegated: one or two implementers partitioned **within** the milestone's single
  repo (by subsystem or disjoint file set). Parallel commits to that repo are
  forbidden even when files differ — partition the work, serialize the commits.
  NEVER partition implementers across repos: a milestone delivers to exactly one
  repo (§ Multi-repo), and an implementer told to commit elsewhere produces a
  commit the evidence layer cannot see.

Use worktrees only for parallel tracked-file mutation. Never isolate an agent
whose inputs/outputs are untracked `.claude/notes/`; the artifacts would be
stranded. Do not push.

Run exploratory project checks from
`data/references/milestone-pipeline-phase-implement.md`; these do not become
closure evidence. The authoritative checks run after rectification through the
detached-worktree `check-run` writer, which preserves every red/green attempt.
Verify no generated deploy repo/file was edited and no forbidden feature branch
was introduced.

Record the complete range (not `HEAD~1`):

```bash
HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)
COMMITS=$(git -C "$REPO_ROOT" rev-list --reverse "$BASE..$HEAD" | python3 -c 'import json,sys; print(json.dumps([x.strip() for x in sys.stdin if x.strip()]))')
BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set "implementation_commit_range=\"$BASE..$HEAD\""
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set "implementation_commits=$COMMITS"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set "implementation_branch=\"$BRANCH\""
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" implement-complete
```

## Multi-repo

**A milestone delivers to exactly ONE source repository.** This is a deliberate
limit, not a gap awaiting a feature. Two repos cannot be pushed atomically, so a
multi-repo milestone can always half-land — and its closure receipt would attest a
delivery that only partially happened. One reviewed diff, one closure receipt, one
publication scope. Multi-*target* operations (environment/account/cluster/resource)
are unaffected and fully supported; only multi-*repo* delivery is refused.

**Split multi-repo work into one milestone per repo, chained with `depends_on`.**
The dependency ships first. That ordering is usually a real operational constraint,
not bookkeeping — e.g. an app must serve its new metrics port before the chart
starts scraping it, or the scrape hits a dead port.

```jsonc
// .claude/notes/roadmaps/<slug>/milestones.json — authored via the roadmap
// doc + roadmap-milestones-merge.py; never hand-write the live register.
{ "id": "<slug>-m4", "repos": ["source/<app>"],  "depends_on": [] },
{ "id": "<slug>-m5", "repos": ["charts/<app>"], "depends_on": ["<slug>-m4"] }
```

**Enforced at three layers, front to back:**

| Layer | Behavior |
|---|---|
| `init-state.sh` | **exit 6**, fresh init only, when the register declares >1 `repos`. Sweeps every register 0–2 levels below each of `$REPO_ROOT`'s ancestors (plus `$PERSONAL_WORKSPACE_ROOT` when set), so it fires regardless of which repo you init from — including depth-1 clones, and registers at the platform root or the workspace root. **Fresh init only: a state that already exists is never re-gated on resume.** No `--override` — the shape is categorically wrong. |
| `roadmap-milestones-status.py` | `depends_on` is DAG-validated; init refuses (exit 3) while a dependency is not `complete`. `--override "reason"` exists here and is audited. **See the visibility caveat below.** |
| `artifacts.py:1661` | `implementation_evidence.repositories` must have exactly one entry. The backstop. |

> ⚠️ **`depends_on` visibility caveat — read this before relying on split ordering.**
> The dependency gate resolves only the register under the **init repo's** own
> `.claude/notes/roadmaps/`, via `roadmap-milestones-status.py --find "$REPO_ROOT"`.
> A register can live in at most one clone, so for a cross-repo split the dependent
> milestone (the one in the *other* repo) **is not dependency-gated** — `init` there
> finds no register and proceeds, even if its `depends_on` target is still `pending`.
> The exit-6 multi-repo refusal is unaffected: it sweeps every register 0–2 levels
> below each ancestor of `$REPO_ROOT`, so it does not share the dependency gate's
> single-clone blindness.
>
> ⚠️ **Resume is never re-gated.** The exit-6 refusal runs on a FRESH init only. A
> multi-repo state created before the gate existed — or by any bypass — resumes at
> `rc=0`, silently, and still wedges at `code-complete`. There is no backward phase
> edge, so such a state can only be discarded, not repaired. If you are resuming a
> milestone whose register declares >1 `repos`, stop and split it now rather than
> spending another phase on it; `artifacts.py:1661` is the only thing that will
> catch it, and by then the state is unrecoverable.
> But the "dependency ships first" ordering above is **advisory, not enforced**, across
> repos. Until that is fixed, order cross-repo splits by hand — and where the ordering
> is operationally load-bearing (e.g. an app must serve a port before the chart scrapes
> it), verify the dependency is actually `complete` before initializing the dependent.

**Why the init gate exists — the failure is unrecoverable, not merely costly.**
`implementation_commits` is writable only in `implement-running`
(`checkpoint.py` field rules) and `PHASE_EDGES` is strictly forward with no
backward edge. So a multi-repo milestone that reaches `implement-complete` has a
frozen, contaminated commit list, `code-complete` refuses it **permanently**, and
the only remedy is to **discard the state**. `dispatcher-receipt-authz-m3` is the
worked example (2026-07-16): it declared two repos in its register, ran research,
implementation and critique, then wedged at `critique-running` and was abandoned —
its commits were fine; only the state was unsalvageable.

**If you are already wedged:** you cannot repair it. Do not hand-edit `state.json`.
Abandon the state (leave it as evidence), split the work into per-repo milestones
with fresh ids, and re-run. The commits themselves are unaffected.

**Symptoms you have hit this:** `implementation_commits` containing a commit that
`git cat-file -e` cannot resolve in the target repo (it lives in a sibling clone —
independent clones do not share object stores); or a `state.json` field encoding
several repos as free text (`source:abc..def charts-x:123..456`). Both are
improvisations around this limit; both produce states that can never validate.

## Step 3 — blind adversarial assessment

Advance and compute the immutable review range:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" critique-running
BASE=$(REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --get implementation_base)
HEAD=$(git -C "$REPO_ROOT" rev-parse HEAD)
```

Every run requires two blind, parallel reviewers:

| Role | Output | Prefix |
|---|---|---|
| `milestone-adversary` | `artifacts/reviews/<role>-<task>-critique.md` | C/H/M/L |
| `milestone-delivery-integrity-adversary` | `artifacts/reviews/<role>-<task>-critique.md` | V-C/V-H/V-M/V-L |

Deterministically add:

- `milestone-frontend-ux` for frontend/TS/JS/Vue/Svelte paths;
- `milestone-infra-safety` for infra paths or a high-risk infra repo identity.

The validator recomputes this selection from the live diff/repo identity; a
manifest cannot omit a selected reviewer.

Dispatch required reviewers in capacity-aware parallel waves. The two
always-on adversaries share the first wave; add conditional roles in that wave
up to the runtime child limit and use a second wave if necessary. Do not put
sibling results in later prompts, and every reviewer remains forbidden to read
sibling critiques before completing its own. They receive only the canonical
role body and ID/base/head/repo/workspace/output inputs. Codex clients without a
named-role selector load and inline the canonical agent body; task names alone
are never treated as role proof.

For the current Codex collaboration API, preselect a globally unique safe task
leaf matching `[A-Za-z0-9][A-Za-z0-9._-]*`, verify that `spawn_agent` returns a
canonical task name whose final component is exactly that leaf, and persist the
unchanged leaf as `agent_task_id`. Do not sanitize or synthesize a different id.
This is prompt-enforced, tamper-evident provenance; it is not a signed runtime
receipt.

For Codex, Claude `tools:` restrictions are prompt policy rather than a runtime
sandbox. Snapshot target HEAD and `git -C "$REPO_ROOT" status --porcelain`
before each review wave and verify both afterward. Any reviewer-created tracked
mutation or ref move invalidates the wave and stops the pipeline.

Before dispatch, persist the exact canonical body snapshot at
`artifacts/reviews/<role>-<task-id>-agent.md` and the exact dispatch prompt at
`artifacts/reviews/<role>-<task-id>-prompt.md` using the
`MILESTONE_REVIEW_DISPATCH_V2` envelope in the artifact contract. Do not append
instructions after the body snapshot. After all returns, lint every critique and
extract all of them in one call:

```bash
F="$WS/.claude/scripts/milestone-pipeline-findings.py"
python3 "$F" extract --check <every critique file>
REPO_ROOT="$REPO_ROOT" python3 "$F" extract --id "$ID" <every critique file>
```

Create `artifacts/review-manifest.json` per
`milestone-review-manifest-v2.schema.json`. It binds the binary/full-index diff,
required roles, declared canonical body source plus snapshot hash, persisted
prompt hash, critique hash, range, times, provider/model, and required runtime
task id. Initialize `closure_reviews` and `operations_reviews` as empty arrays.
Record the exact critic set and findings path in
state, then advance:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'critics_run=[...]'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'critique_files=[...]'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'critique_path=".claude/notes/milestones/<ID>/artifacts/reviews/<adversary-critique>.md"'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set 'critique_finding_counts={"critical":0,"high":0,"medium":0,"low":0}'
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" --set "findings_register=\".claude/notes/milestones/$ID/findings.json\""
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" critique-complete
```

`critique-complete` hash-binds all receipts. Missing/malformed reviewers leave
the phase running.

## Workflow port — the sanctioned way to run Step 3

Step 3's reviewer fan-out has a canonical **Workflow-tool port**. Prefer it over
hand-driving the dispatches: it exists because ad-hoc inline Workflow dispatch
produces the *work* but none of the *assessment artifacts* this state machine
demands, and `critique-complete` then cannot bind (observed 2026-07-16,
`dispatcher-receipt-authz-m3a`: reached `rectify-running`, could never reach
`code-complete` — no body snapshots, no persisted prompts, no task ids, no
manifest).

**Why the port is worth it — it removes the second AUTHOR of the prompt.** A
human orchestrator persists a prompt file and *separately* composes the dispatch:
two sources, so they can silently diverge, and the validator (which hashes the
FILE) still passes — voiding the lane unrepairably (dna-rem-m6, 2026-07-15). The
port makes the prompt ONE source: `…-review-prepare.py` writes it **and** emits
those same in-memory bytes on stdout; the `.mjs` dispatches
`args.reviewers[i].prompt` without re-composing it.

**What the port does NOT close — know this before you trust the provenance.**
The prepare JSON reaches `Workflow({args})` only because *you*, the orchestrating
model, re-emit a 20-43KB blob inside a tool call. There is no file-based args
mechanism (`pipeline-pattern-v2.md` §2: args are model-composed). A compaction
that truncates the body tail is invisible to the manifest, which hashes the
pristine FILE. That hop is **covered by a tripwire, not closed by construction**:
prepare emits `prompt_len_utf16` + `prompt_checksum`, and the `.mjs` recomputes
both over the args value immediately before `agent(...)` and **throws**, naming
the reviewer and expected-vs-actual. So a mangled blob fails LOUDLY and
pre-dispatch instead of producing void provenance — but a harness that mutated
the string between the tripwire and the model call would still be outside the
closure, and nothing here proves what the reviewer *received*. Do not describe
this hop as one that "cannot diverge".

Exactly three calls, after `critique-running`:

```bash
S="$WS/.claude/scripts"
# 1. persist the wave's artifacts, emit their bytes + the leg-3 tripwire values
python3 "$S/milestone-pipeline-review-prepare.py" --id "$ID" \
  --repo-root "$REPO_ROOT" --stage assessment > /tmp/prepare-$ID.json

# 2. dispatch those bytes — blind, parallel. Pass the prepare JSON THROUGH
#    UNMODIFIED (plus id + repoRoot); never rebuild reviewers[].prompt, and never
#    drop required_reviewers/prompt_len_utf16/prompt_checksum — the .mjs throws
#    without them rather than skipping the checks they drive.
Workflow({ scriptPath: "data/scripts/milestone-pipeline-workflow.mjs",
           args: { ...<contents of /tmp/prepare-$ID.json>,
                   id: "<ID>", repoRoot: "<REPO_ROOT>" } })
# -> save its return value to /tmp/workflow-$ID.json

# 3. verify the wave and bind the receipts (refuses; never writes what it cannot validate)
python3 "$S/milestone-pipeline-review-manifest.py" --id "$ID" \
  --repo-root "$REPO_ROOT" \
  --prepare-result /tmp/prepare-$ID.json \
  --workflow-result /tmp/workflow-$ID.json
```

Then checkpoint with the values the manifest builder emitted under
`checkpoint_values` (`critics_run`, `critique_files`) plus `critique_path`,
`critique_finding_counts`, and `findings_register` as in Step 3 above, run the
`findings.py extract --id` register write, and advance `critique-complete`.

| Script | Authority |
|---|---|
| `milestone-pipeline-review-prepare.py` | selects reviewers (imports `_required_reviewers` — one source), snapshots each canonical body **at `state.agent_kit_commit`**, builds + persists the `MILESTONE_REVIEW_DISPATCH_V2` prompt, emits those same bytes plus their `prompt_len_utf16`/`prompt_checksum`, snapshots pre-wave HEAD/worktree + a census of `artifacts/reviews/` |
| `milestone-pipeline-workflow.mjs` | re-measures `reviewers[i].prompt` against the tripwire values and **throws** on divergence (the model-composed args hop is not harness-attested), then dispatches it parallel + blind; throws on an absent `required_reviewers`, or a null/mismatched reviewer (never `.filter(Boolean)`) |
| `milestone-pipeline-review-manifest.py` | cross-checks `--id` against the prepare result, verifies the wave invariant + the reviews-dir census + every hash/marker, refuses a zero-findings critique, writes `review-manifest.json` only after schema validation |

**What stays in the main session — not portable, by design:**

- **Step 0–2** (init, research, implement) and **every checkpoint**: a workflow
  has no filesystem and cannot run `checkpoint.py`.
- **Step 4 rectify + closure**, **Step 5 publication**, **Step 6 apply/verify**:
  each contains a human authorization boundary, and **a workflow cannot prompt
  the user for an external-write confirmation**. Starting a workflow is never
  push permission.
- The port covers the **assessment** stage only. Closure and operations receipts
  have different stage-bound headers and inputs; they are still hand-dispatched
  and appended via `review-append`.

## Step 4 — rectify and independently close

Advance to `rectify-running`. The main session rectifies by default; use
`milestone-rectifier` only as an exception. The implementer must not certify its
own fixes.

1. Read every critique fully.
2. Re-verify every C/H at cited lines before changing code.
3. Fix every confirmed C/H and its regression guard. Handle M/L per the
   canonical rectification policy.
4. Re-run exploratory checks, commit locally, then run every authoritative
   check through `milestone-pipeline-artifacts.py check-run`. The runner uses a
   fresh detached worktree, installs Node dependencies from the reviewed
   lockfile with frozen/ignore-scripts policy, rejects ambient/untracked source
   inputs, hashes executable and tracked script inputs, and ledgers failures as
   well as passes. A marker-less repository must declare exact commands in the
   reviewed `.milestone-pipeline/checks.json` contract. Do not push.
5. Use `milestone-pipeline-findings.py set` for every disposition. A nonempty
   resolution string is not proof by itself; the closure verifier re-opens the
   code and commit history.
6. Run the findings gate. Exit 3 means stop and rectify more.

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" rectify-running
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-findings.py" gate "$ID"
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" check-run \
  --state "$STATE" --name "<required-check-name>" -- <command> <args...>
```

Set either `rectification_commit` or, only for a truly zero-change closure,
`rectification_not_required_reason`—never both.

Now snapshot the findings register and dispatch `milestone-closure-verifier`
against the final head, assessment manifest, active passing checks, and the
complete append-only check-attempt ledger. It is read-only and cannot repair a
failure. Give every attempt unique body/prompt/report/snapshot paths. Append
the receipt only through the locked writer; a FAIL remains immutable history
and a later task may append a new receipt after rectification. Only the latest
attempt may bind current inputs and only `PASS` can advance:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append \
  --state "$STATE" --stage closure --receipt "$CLOSURE_RECEIPT"
```

Only after closure returns PASS, create `implementation-evidence.json` with every repository/range/commit,
passing check and output evidence, current review/findings hashes, rectification
claim, closure report hash, and generated/render checks. Then:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" validate implementation_evidence \
  "$REPO_ROOT/.claude/notes/milestones/$ID/artifacts/implementation-evidence.json" --state "$STATE"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" code-complete
```

At this point say **code complete**, never **complete**, unless both delivery
axes were explicitly marked not-required with reasons before this transition.

## Step 5 — publication (external-write gate)

If publication is required, advance to `publish-running`. Starting this
pipeline is not push permission. The deterministic publication writer must
discover and freeze the exact remote/ref/precondition/action scope before any
human decision:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" publish-running
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-preview \
  --state "$STATE" --mode publish
```

Present the returned scope, scope hash, isolated execution environment, remote
precondition, and proposed action without paraphrasing it. Only after the human
approves that exact scope may the writer persist authorization and execute it:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-authorize \
  --state "$STATE" --mode publish --approved-by "$HUMAN" \
  --scope-hash "$SCOPE_HASH"
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" publication-apply \
  --state "$STATE"
```

When the exact reviewed commit is already the remote head, use
`--mode adopt-preexisting` for both preview and authorization. This is a human
acknowledgement of observed state, not permission to push. The writer uses a
state-owned isolated `HOME`, a configless bare push repository backed only by
the reviewed object directory, disabled ambient Git config/hooks, a
compare-and-swap precondition, append-only intent supersession, and a durable
execution receipt. A failed or ambiguous mutation must be re-observed; never
replay it speculatively or hand-edit its intent.

After successful publication or adoption, create `release-manifest.json`:

- bind every source revision to a declared remote/branch and observed commit;
- prove source ancestry via a supported remote/provider method;
- for GitOps/mixed delivery, record the expected rendered revision(s) — one
  `rendered_revisions[]` entry per deploy repo (a chart that fans out into
  `argocd-config-commercial` + `argocd-config-commercial-mono` lists both);
- when the render was triggered by an image-tag bump in the chart repo, declare
  that hop in `intermediate_revisions[]` (`role: "chart-bump"`, `binds_image_tag`
  = the source short-sha) so the rendered revision — whose provenance names the
  *chart* as its source — still binds to the reviewed Go source;
- for artifact/mixed delivery, resolve an immutable SHA-256 digest. When the
  renderer records no digest (platform `artifacts: []`), a released digest is
  bound at release time by resolving the chart-bump image tag through `crane`.

Emit `gitops`/`mixed` only once the downstream cascade has actually landed for
this source (the live deploy render carries this source's image) and set
`operations_required: true`; the live-delivery proof (Synced/Healthy at the
desired revision, pod image digest) is the operations phase, not `published`. If
the cascade has not yet landed, stay `source-only` and defer.

The reviewed source commit must contain
`.milestone-pipeline/trust-policy.json`, binding the exact source origin,
allowed rendered-repository and registry prefixes, and the absolute SHA-256
identity of the artifact resolver. Environment overrides and arbitrary PATH
resolvers are rejected; artifact delivery accepts only a hash-bound
system/package-manager `crane` binary with the exact `digest URI` contract.
GitOps/mixed delivery also requires every rendered commit to carry a
machine-owned source-revision provenance blob at its declared `provenance_path`
(the platform's `gitops-provenance.py` writes `.workspace/source-revisions/<app>.json`;
the framework `milestone-render-provenance.py` emits the same claim schema).

Generic or inferred auto-sync remains forbidden. A schema-v2 trust policy may
preauthorize exactly one of two finite cascades, discriminated by `kind`:
`ci-render-argocd-auto-sync-v1` (one hash-bound GitLab CI renderer → one
protected render remote/branch → one enumerated Argo Application edge per
target), or `ci-render-argocd-auto-sync-fanout-v1` for the platform
source → image → chart-bump → N-deploy-repo fan-out (an `image_build` hop, an
intermediate `chart` hop, one protected `render_legs[]` entry per deploy repo,
and one Argo auto-sync per target bound to its `render_leg_id`). The preview
includes that material delivery effect in the human-authorized scope, revalidates
each leg's live render head and every target's live Application
UID/config/CA/source/destination/automated policy immediately before the CAS
push, and refuses omitted/unknown steps or target drift. `adopt-preexisting`
cannot retroactively authorize a cascade that already happened.

Validate the manifest and then advance:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" published
```

If operations are explicitly not required, `published -> complete` is legal
only with `operations_not_required_reason` set before code-complete.

## Step 6 — plan, authorize, apply, and verify

For required operations, author `operations-plan.json` first. Delivery-state
v2 supports a manual path and one exact preauthorized-controller observation
path:

- `apply_method: gitops-manual-sync`, implemented as an exact revision-pinned
  `argocd app sync` against a frozen Argo server;
- `apply_method: gitops-auto-sync-observe-v1`, only when its target and active
  verification-action hash exactly match the publication delivery effect. It
  has no apply command; the writer only observes the already-authorized Argo
  controller result with `attempt-adopt-auto-sync`;
- `verification_profile.kind: argocd-web-workload-v1`, which binds and parses
  the exact Argo Application, Kubernetes Deployment, selected Pods, Service,
  Kubernetes Ingress, and credential-free HTTPS smoke URL;
- typed probes prove Argo Synced/Healthy at the desired rendered revision,
  Deployment observed generation, Pod image digest when declared, Service to
  workload selection, exact Ingress-to-Service routing, and the exact expected
  HTTP 2xx smoke status;
- `argocd-istio-internal-http-v1`, which proves an exact
  `<service>.<namespace>.svc.cluster.local` Service, ready EndpointSlices, a
  bound in-mesh caller/sidecar, and a bounded HTTP 2xx service smoke; and
- `argocd-istio-eastwest-v1`, which separately proves the tenant-cluster
  `.global` host, sender/receiver ServiceEntries and DestinationRules, receiver
  EnvoyFilter/gateway identity, sender and receiver xDS/endpoints, and a bounded
  sender-side HTTP 2xx smoke. Its sender/receiver API servers must be distinct;
  the receiver mesh and workload contexts must bind the same server/CA, and
  Istio proxy queries use qualified `pod.namespace` identities.

The two Istio profiles are internal routing proofs, not public hostname
definitions: they do not replace the public
`{app}.{tenantpostfix}.{environment}.example.com` convention and cannot borrow an
Ingress proof. Kargo, Crossplane, Keycloak, control-plane workloads, Pulumi,
provider APIs, direct `kubectl` mutations, and source-backed operational
wrappers remain unsupported and must fail closed. Never relabel a generic
command as a named probe.

Each target also binds desired source/render/digest, immutable execution
contexts and trust roots, owners, rollback, exact SHA-256-bound argv, bounded
timeouts, and maximum evidence age. Compute the canonical plan and target scope
hashes with the artifact tool.

Both context files must be regular, non-symlinked JSON and are re-hashed and
re-parsed immediately before each command. The Kubernetes context binds the
exact `--kubeconfig`/`--context`, HTTPS cluster server, embedded CA hash, and
forbids executable/auth-provider/token-file plugins. The Argo context binds the
exact `--config`/`--argocd-context`/`--server`, selected server/user, embedded
CA, and whole config-file hash (including its selected auth token). Credential
bytes are used only by the tools and are never persisted in evidence.

Advance to `plan-review-running`, snapshot the exact release and plan, and
dispatch the independent `milestone-operations-adversary`. Append every FAIL
or PASS through `review-append`; only the latest PASS can freeze the plan:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" plan-review-running
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" review-append \
  --state "$STATE" --stage operations --receipt "$OPERATIONS_RECEIPT"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" plan-reviewed
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" apply-running
```

Ask one unambiguous target-scoped action at a time. First render the exact
action and scope from the frozen plan:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-preview \
  --state "$STATE" --target "$TARGET"
```

Only after the human approves the returned action and scope hash may the writer
record `human-explicit` authorization and create an attempt:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-start \
  --state "$STATE" --target "$TARGET" --approved-by "$HUMAN" \
  --scope-hash "$SCOPE_HASH"
```

For an exact `gitops-auto-sync-observe-v1` target, preview reports that no
second apply authorization is required because publication already authorized
the full conditional effect. Do not call `attempt-start` or `attempt-apply`;
run the non-mutating observer instead:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-adopt-auto-sync \
  --state "$STATE" --target "$TARGET" --collector "$COLLECTOR"
```

It appends either a converged `observed-auto-sync-v1` attempt or a failed
observation. It never invokes Argo sync, and a retry is allowed only after a
failed observation.

Invoke `attempt-apply --actor ... --collector ...`; never execute the plan
command separately and never hand-edit or replace an attempt. The writer itself
executes the frozen apply argv, captures its output, runs the frozen post-apply
observation, compares observed identity to desired identity, preserves
predecessor hashes, and refuses non-latest mutation. Callers cannot attest their
own status, observed JSON, exit code, or evidence file.
After every target has a matching applied attempt:

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" applied
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" verify-running
```

Verification is per target and fresh under the typed plan contract. ArgoCD
Synced/Healthy is only one fact in the selected public-Ingress,
same-cluster-Service, or east-west-Istio behavior graph. Wrong identity, wrong
UID/destination/source path, wrong digest, disconnected routing, missing smoke,
stale evidence, a failed target, or a partial multi-target rollout blocks
advancement.

Use `attempt-preview --attempt-id ...` before verification. Initial verification
of a pending applied attempt introduces no new delivery mutation and needs no
new authorization; any bounded active smoke was already in the authorized
target surface. Run
`attempt-verify --state ... --target ... --attempt-id ...`. The writer executes
the frozen typed collectors itself, rechecks executable/config hashes, persists
only projected non-secret facts, and derives identity/probe status. Callers
cannot submit their own output, status, or exit codes.

Refreshing an already terminal or stale verification is a separately scoped
live observation. Preview it, present the exact returned scope, and pass
`--approved-by` plus `--scope-hash` only after explicit human approval:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-preview \
  --state "$STATE" --target "$TARGET" --attempt-id "$ATTEMPT"
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify \
  --state "$STATE" --target "$TARGET" --attempt-id "$ATTEMPT" \
  --approved-by "$HUMAN" --scope-hash "$SCOPE_HASH"
```

The refresh authorization intent is committed before collectors execute. If a
process dies with an unresolved intent, close it without replaying commands:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" attempt-verify-recover \
  --state "$STATE" --target "$TARGET" --refresh-id "$REFRESH_ID"
```

Recovery records `ambiguous`; obtain a new preview and authorization for any
later refresh. Use `waiver-append` only after explicit human approval and only
for the exact current verification gaps; identity mismatch and the whole
verification contract are never waivable.

If verification fails, transition back to `apply-running` and use
`attempt-start` to append the newly authorized attempt; never rewind to
code-complete. Waivers must be target/scope bound, human-approved, active,
compensating, and name a follow-up milestone. Expired entries remain immutable
history but cannot satisfy a gate.

```bash
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" operationally-verified
```

## Step 7 — terminal reconciliation

Run blocking reconciliation; do not append `|| true`:

```bash
python3 "$WS/.claude/scripts/milestone-pipeline-artifacts.py" reconcile --state "$STATE"
REPO_ROOT="$REPO_ROOT" python3 "$WS/.claude/scripts/milestone-pipeline-checkpoint.py" "$ID" complete
```

After `complete` succeeds, emit one Phase 5 artifact receipt for the finalized local evidence set:

```bash
if [ -f "$WS/scripts/artifact_skill_capture.py" ]; then
  python3 "$WS/scripts/artifact_skill_capture.py" emit \
    --workspace "$WS" --producer milestone-pipeline --run-id "$ID" \
    --root "$REPO_ROOT/.claude/notes/milestones/$ID" --apply
fi
```

This append-only receipt does not write to Qdrant or Graphiti; Graphiti bulk ingestion remains
disabled. Do not emit it before a successful terminal gate. Capture failure must not alter delivery
state, trigger evidence rewrites, or delete prior receipts; report
`ingestion receipt: created|idempotent|unavailable|failed` in the final handoff.

Only after the gate may a roadmap/register projection render the overall
milestone complete. `complete` is an audited snapshot, not an eternal live
claim: status revalidates freshness and `complete -> verify-running` is the
governed reopen edge when required operational evidence becomes stale. A new
apply is authorized only when verification establishes that mutation is
actually required. Human
authored scope fields remain frozen. The state/artifacts remain the delivery
authority; a generated checkbox cannot override them.

Capture the run outcome best-effort after completion, but never let telemetry
failure alter delivery state. If the provider lacks token statistics, record
null rather than inventing a value.

## State graph

```text
init -> research-running -> research-complete
     -> implement-running -> implement-complete
     -> critique-running -> critique-complete
     -> rectify-running -> code-complete
     -> publish-running -> published
     -> plan-review-running -> plan-reviewed
     -> apply-running -> applied -> verify-running
     -> operationally-verified -> complete

code-complete -> complete  (publication=false AND operations=false, both reasoned)
published -> complete      (operations=false, reasoned)
applied/verify-running -> apply-running  (append-only retry)
operationally-verified -> verify-running (refresh live verification)
complete -> verify-running (required live evidence stale)
```

The checkpoint uses an explicit adjacency graph, not numeric ordering.

## Script index

All scripts live under `$WS/.claude/scripts/`; invoke Python with `python3`.

| Script | Authority |
|---|---|
| `milestone-pipeline-init-state.sh` | fresh schema-v2 state + dependency/deploy-repo gates |
| `milestone-pipeline-migrate.py` | explicit one-way backup-first v1 migration |
| `milestone-pipeline-checkpoint.py` | locked legal transitions, machine-owned derived state, artifact receipt persistence |
| `milestone-pipeline-artifacts.py` | strict artifact semantics, plan/scope hashes, append-only/freshness checks, reconciliation |
| `milestone-pipeline-status.sh` | dual-axis status, phase history, bound receipts |
| `milestone-pipeline-findings.py` | critique parsing, findings register, dispositions, C/H gate |
| `milestone-pipeline-review-prepare.py` | Step-3 Workflow port: persists the wave's body/prompt artifacts, emits those same bytes + the leg-3 length/checksum tripwire values |
| `milestone-pipeline-workflow.mjs` | Step-3 Workflow port: re-measures the args prompt against the tripwire (throws on divergence), then blind parallel dispatch |
| `milestone-pipeline-review-manifest.py` | Step-3 Workflow port: wave-invariant + hash verification, then binds `review-manifest.json` |

## Non-negotiable guards

- Never infer operational verification from prose, a push, CI, ArgoCD sync, or
  a writer-supplied freshness timestamp.
- Never run a terminal reconcile with `|| true`.
- Never let one adversary stand in for the two always-required blind lanes.
- Never claim a Codex task name proves a custom-agent body ran; bind the loaded
  per-run body snapshot and exact prompt hashes, and describe that as
  tamper-evident provenance rather than runtime-signed execution proof.
- Never let the implementer or rectifier self-certify closure.
- Never mutate generated `deploy/argocd-config-*`; fix source.
- Never push/apply because the user asked to start or finish the pipeline; each
  external mutation needs a concrete explicit authorization.
- Never publish without `publication-preview` -> exact-scope human authorization
  -> `publication-apply` (or the equally explicit `adopt-preexisting` path).
- Never start a manual apply without `attempt-preview` and exact target-scope
  human authorization; never refresh verification without its own durable
  intent. Auto-sync adoption is legal only for the exact publication-bound
  observer path and never executes an apply command.
- Never use a generic command or an unsupported platform as if it satisfied a
  typed web, same-cluster Service-FQDN, or east-west Istio graph.
- Never discard a failed apply/verification attempt; append and hash-chain the
  next attempt.

## Invocation examples

```text
/milestone-pipeline ISSUE-1234
/milestone-pipeline W29 --deep
/milestone-pipeline W29 --resume
$milestone-pipeline W29 --repo-root /abs/target/repo --resume   # Codex adapter
```
