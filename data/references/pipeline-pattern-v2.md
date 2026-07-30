---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Pipeline pattern v2 — generations, the Workflow-tool contract, worktree doctrine, fallbacks

**Read this before running, debugging, or building any `/…` pipeline command.** The 13 slash
commands in `data/commands/` are NOT one architecture — two generations coexist plus a meta tier,
and different rules apply to each. This file is the single home for the cross-pipeline doctrine;
the per-command bodies cross-reference it instead of restating it.

Sibling docs (one home per fact — do not restate their content here):

| Fact | Home |
|---|---|
| CWD / env contract, `PERSONAL_WORKSPACE_ROOT`, python3-not-bash rule, model floor | `data/references/runtime-contract.md` |
| Agent-memory model (`memory: project` is a hint; file I/O is load-bearing) | `data/references/agent-memory.md` |
| Outcome-log record schema + emit semantics | `data/references/pipeline-outcome-log-schema.md` |
| Milestone delivery state, artifacts, typed operations, and legal edges | `data/references/milestone-pipeline-state-schema.md` and `data/references/milestone-pipeline-artifacts-v2.md` |
| Gen-1 scaffold file templates (init.sh / checkpoint.py / status.sh / agents) | `data/references/pipeline-builder-prompt.md` |
| Skill→command conversion gotchas (14-item catalog) | `data/references/skill-to-command-conversion-prompt.md` |

---

## 1. Generation map (which command follows which rules)

| Generation | Commands | Orchestration | State mechanism | External-write gate |
|---|---|---|---|---|
| **Gen-1 — main-session orchestrator** (main session drives every dispatch) | `/milestone-pipeline` plus Codex `$milestone-pipeline`, `/spike`, `/roadmap`, `/argoops` | Provider adapter maps the canonical body to main-session subagent fan-out; milestone has reviewed Claude-command and Codex-skill adapters | milestone delivery-state v2 uses hash-bound JSON artifacts + adjacency checkpoint; other Gen-1 commands retain their listed mechanisms | **Owned by the main session** — the orchestrator pauses at every push / issue / cluster mutation |
| **Gen-2 — background Workflow port** | `/capability-scout`, `/cicd-uplift`, `/frontend-uplift`, `/interop-discovery`, `/mesh-as-code`, `/zerotrust-scout` | Main session does only Step 0 (init/inventory/preflight) + final present; a `data/scripts/<name>-workflow.mjs` runs Phases 1–4 in the background via the **Workflow tool** | Workflow journal (resume via `resumeFromRunId`); the legacy `<name>-checkpoint.py` machine is retained only for the superseded markdown path (+ Phase-0 inventory bookkeeping in cicd-uplift / interop-discovery / mesh-as-code) | These pipelines are read-only by design (NEVER ship code, NEVER auto-invoke downstream); the only gates are the handoff OFFERs in the main session |
| **Meta — build / convert / promote** | `/pipeline-builder`, `/skill-to-command`, `/memory-sync` | Main session (short, turn-based builds) | File system is the state | Commit is local; push and wiki writes are explicitly gated |

**Which shape does a NEW pipeline get?** (encoded in `/pipeline-builder`):

- **Gen-2 (default for discovery-style pipelines)** — read-only fan-out → synthesize → challenge →
  prioritize, no user interaction between phases, no external writes mid-run. The Workflow port
  keeps scout briefs and max-effort reasoning off the main session and costs 0 model tokens for
  control flow.
- **Gen-1 (required for gated-ops pipelines)** — any pipeline where the MAIN SESSION must own an
  external-write gate mid-run (e.g. `/argoops` remediation: per-fix human confirmation between
  diagnosis and apply), or where a phase needs user interaction (`AskUserQuestion`, browser
  handshakes) mid-pipeline. A background Workflow **cannot prompt the user, cannot exec scripts,
  and cannot pause for confirmation** — so gated-ops pipelines stay Gen-1.
- Sequential build pipelines (research → implement → critique → rectify/close →
  publish → apply → verify, i.e. the milestone shape) stay main-session
  orchestrated. Mutation authorization cannot move to a background Workflow.
  The Codex adapter is a generated skill, not a background-runtime port.

### Milestone delivery-state v2 invariants

The milestone pipeline exposes separate machine claims for `code-complete`,
`published`, `applied`, and `operationally-verified`; `complete` is a reconciled
snapshot of the claims required for that milestone. A green checklist or local
commit cannot imply publication, and publication cannot imply live apply or
verification. If required verification later becomes stale, the governed edge
is `complete -> verify-running`, not an automatic re-apply.

Every nontrivial implementation has two independent blind assessment lanes:
`milestone-adversary` and `milestone-delivery-integrity-adversary`, plus the
deterministically selected frontend/infra roles. An independent closure verifier
must pass after rectification. Required operations add an independent operations
adversary against frozen release/plan snapshots. These are artifact-gated
requirements, not optional orchestration advice.

Every external effect remains a main-session human boundary:

1. publication uses read-only `publication-preview`, exact-scope human
   authorization, then `publication-apply` or explicit `adopt-preexisting`.
   A schema-v2 automatic GitOps declaration is legal only when this same scope
   enumerates the complete source -> GitLab CI render -> protected render branch
   -> per-Application Argo cascade before the source push; generic or inferred
   auto-sync is forbidden;
2. manual mutation uses `attempt-preview`, exact target-scope human
   authorization, then the deterministic attempt writer. For a preauthorized
   automatic target, `attempt-adopt-auto-sync` only observes controller
   convergence and cannot execute or replay a sync;
3. repeat live verification uses a new preview and authorization whose intent
   is durably written before collectors run; ambiguous refreshes are recovered
   without replay;
4. changing the frozen pipeline kit uses `kit-upgrade-preview` and its own
   exact-scope human authorization.

The operational contract is intentionally not generic. V2 can mutate only
through exact revision-pinned Argo sync; its automatic branch performs no
mutation and requires the preauthorized publication cascade. It verifies one
of three non-interchangeable graphs: `argocd-web-workload-v1` for public
Ingress, `argocd-istio-internal-http-v1` for exact same-cluster Service FQDN,
or `argocd-istio-eastwest-v1` for the complete sender/receiver `.global` Istio
route. Internal bounded `kubectl exec` smoke is an active action whose exact
surface is publication-bound, not an ungoverned read. Kargo, Crossplane,
Keycloak, control-plane workloads, Pulumi, provider APIs, direct `kubectl`
mutations, and source-backed operational wrappers remain fail-closed. Do not
turn a named probe into a label over an arbitrary command.

Internal `.svc.cluster.local` and `.global` routing identities remain separate
from the public `{app}.{tenantpostfix}.{environment}.example.com` hostname
contract.

Its execution scope hash-binds and rechecks JSON-form kubeconfig and Argo config
files, selected contexts/servers, embedded CAs, and the Argo config file that
contains the selected auth token. Commands must name the exact `--kubeconfig`,
`--context`, `--config`, `--argocd-context`, and `--server` values. Evidence
never persists credential bytes. Publication similarly executes in a
state-owned isolated `HOME` through a configless bare push repository rather
than the source checkout's Git configuration.

---

## 2. The Workflow-tool contract, as used by the six live `.mjs` files

**Status: harness-provided API. Verified against all six `data/scripts/*-workflow.mjs` on
2026-07-02.** This is not a documented public Anthropic API — it is the surface these six files
actually depend on, extracted from their code. If the harness changes this contract, these files
break together; re-verify here first.

A workflow file is a plain `.mjs` module executed by the harness's **Workflow tool**:

```
Workflow({
  scriptPath: "data/scripts/<name>-workflow.mjs",   // relative to the agent-kit checkout
  args: { id: "<ID>", brief: "<BRIEF>", ... }       // pipeline-specific
})
// Resume a partial run (completed phases return cached results):
Workflow({ scriptPath: "data/scripts/<name>-workflow.mjs", resumeFromRunId: "<prior run id>" })
```

Inside the `.mjs`, the harness provides these globals (no imports exist in any of the six files):

| Surface | Shape (as used) | Notes |
|---|---|---|
| `export const meta` | `{ name, description, phases: [{ title, detail }] }` | Drives the `/workflows` live tree display |
| `args` | implicit global; may arrive as an object, a JSON string, or absent | All six files defensively parse: `const _args = (typeof args === 'string') ? (args.trim() ? JSON.parse(args) : {}) : (args || {})` |
| `phase(title)` | marks the current phase (must match a `meta.phases[].title`) | |
| `log(message)` | progress line into the workflow journal | |
| `agent(prompt, opts)` | dispatches ONE sub-agent; `await`-able; returns the structured result object or `null` on failure | `opts`: `agentType` (registered agent name — required in practice), `label` (journal display), `phase`, `schema` (strict JSONSchema for the return, `additionalProperties: false` throughout), `model` (optional override — pass `undefined` to inherit the agent's stamped frontmatter). **`effort` is NOT passed as an option in any live file** — effort always comes from the agent's stamped frontmatter. |
| `parallel([...thunks])` | takes an array of **zero-arg functions** each returning an `agent(...)` promise; awaits all | e.g. `await parallel(scouts.map(s => () => agent(...)))` |
| top-level `await` / `return` | the module's `return` value is the compact structured result the main session receives | Live shape: `{ id, scouts_returned, candidate_count, challenge_counts: {critical,high,medium,low}, final_report_path, ranked, ... }` |

Hard constraints (stated in every live file's header comments, grep-verified):

- The Workflow JS has **NO filesystem or exec access** — it cannot run `init.sh`, `checkpoint.py`,
  preflight scripts, or kubectl. All file I/O happens inside the dispatched agents. That is why
  Phase-0 init/inventory/preflight ALWAYS stays in the main session (Step 0/0b of each command).
- A workflow **cannot prompt the user** — every gated OFFER/confirm stays in the main session.
- Paths inside the `.mjs` (e.g. `.claude/notes/<name>s/<id>/…`) are relative strings passed to
  agents in prompts; the agents resolve them from their own CWD. Run the Workflow with the CWD at
  the agent-kit checkout (see §6).

---

## 3. Availability check + fallback protocol (no Workflow tool in your session)

The Workflow tool is **harness-build-specific**: it exists on the harness these six pipelines were
built and run on (live run journals exist under `.claude/notes/capability-scouts/` etc.), but it is
NOT present in every Claude Code environment — some sessions have neither the tool nor a deferred
entry for it.

**Check before dispatching:** is a `Workflow` tool listed in your session's tool inventory (or in
the deferred-tools list, loadable via ToolSearch)? If yes, proceed per the command body.

**If absent:**

| Pipeline class | What to do |
|---|---|
| All Gen-1 (`/milestone-pipeline`, `/spike`, `/roadmap`, `/argoops`) | Unaffected — they never touch the Workflow tool. Run normally. |
| Gen-2, quick-answer need | Run the standalone snapshot skill instead: `/interop-inventory` (for `/interop-discovery`), `/mesh-snapshot` (for `/mesh-as-code`). These produce the deterministic inventory/matrix without the multi-agent phases. |
| Gen-2, full-pipeline need | **Defer** — note the run request and execute it from a Workflow-capable session. **Do NOT re-inline SYNTHESIZE/PRIORITIZE into the main session** (every Gen-2 command's Don'ts forbid it: the offload IS the point — briefs and max-effort reasoning stay off the main thread). Re-inlining also produces a run with no journal, no resume, and a blown main-session context. |
| Gen-2, Phase 0 only | The Phase-0 inventory scripts (`cicd-uplift`, `interop-discovery`, `mesh-as-code`) are plain main-session bash — they run fine without the Workflow tool and their digests remain useful standalone. |

---

## 4. Worktree doctrine (RESOLVED — one rule, no per-file variants)

Three files used to state three different rules. The resolved rule:

> **Use `isolation: worktree` ONLY when two or more agents run in parallel AND mutate tracked
> files in the same git repo.** Worktrees exist to keep concurrent tracked-file edits from
> trampling each other — nothing else.
>
> **NEVER use worktree isolation for any agent whose read or write surface lives in untracked
> `.claude/notes/`** (pipeline state dirs, inventory digests, briefs, critique files). A git
> worktree does NOT carry untracked files: the agent cannot read the inventory it needs, and its
> output lands in a throwaway worktree and is lost.

Applied to the live pipelines:

| Site | Verdict |
|---|---|
| Gen-2 scouts / synthesizer / challenger / prioritizer | **No worktree, ever.** Read+write surfaces are untracked `.claude/notes/…`; they are read-only against the repo and write to disjoint per-role paths, so there is no tracked-file race to isolate. |
| `/skill-to-command` Turn 2 (2 parallel authors) | **Worktree is legitimate** — two agents mutate tracked `data/…` files in the same repo concurrently. (Their write surfaces are disjoint by contract, so shared-cwd also works; worktree is the belt-and-braces choice IF the harness supports the parameter.) |
| `/skill-to-command` Turn 3 reviewer, `/pipeline-builder` reviewer | **No worktree** — they write the critique to untracked `.claude/notes/<name>-build-critique.md`, which would be stranded in the worktree. |
| `/pipeline-builder` scaffolder | **No worktree** — it runs alone (no parallel mutation to isolate), and its inline smoke test writes to untracked `.claude/notes/<name>s/smoke-test/`. |
| `/milestone-pipeline` parallel implementers | **Worktree is legitimate** (parallel tracked-file mutation) — but note the stronger rule that supersedes it: partition parallel implementers **by repo**, not by files; worktrees alone do not make same-repo concurrent commits safe (the index races). |

Additional caveat (from `/milestone-pipeline`): the `Agent` tool's `isolation: "worktree"`
parameter is a harness feature with **unverified support across harness versions**, and it requires
the orchestrator's CWD to be inside a git repo (the workspace root is NOT one — see
`runtime-contract.md`). If unsupported, fall back to explicit `git worktree add` / `remove` in
bash, or run sequentially in the shared cwd.

---

## 5. First-run agent-registration caveat (canonical home)

The `Agent` tool's `subagent_type` registry is loaded at **Claude Code session start**. Any agent
file created (or pulled) during the CURRENT session — e.g. by `/pipeline-builder`,
`/skill-to-command`, or a fresh `git pull` — will fail dispatch with "Agent type not found" until
the session is **restarted**.

Interim fallback: dispatch `subagent_type: general-purpose` and inline the agent's body (role,
input variables, read-list, scope-bounds) from `data/agents/<name>-<role>.md` into the dispatch
prompt — behaviourally equivalent, minus the registered name and any frontmatter model stamp. On
any later session the named agents resolve directly.

This applies to **every** pipeline family. Command bodies cross-reference this section instead of
restating it.

---

## 6. CWD + script-path contract (cross-reference)

The full CWD/env contract is owned by `data/references/runtime-contract.md`. The per-generation
summary (each command body also states its own requirement explicitly):

| Command family | Required CWD | Script-path idiom |
|---|---|---|
| Claude `/milestone-pipeline`, `/spike`, `/roadmap` | Inside the **target repo** (one of the ~70 independent clones — the workspace root, `repos/`, and `<project>/` are NOT git repos, so `git rev-parse` fails there) | `"$WS/.claude/scripts/<name>-*.{sh,py}"` where `WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"` |
| Codex `$milestone-pipeline` | Start Codex at the **the workspace root** so `.agents/skills` and `.codex/agents` are discovered; pass absolute `--repo-root` and use `git -C` | Same `$WS/.claude/scripts/...` shared runtime. Starting inside a nested target git root can hide workspace adapters. |
| `/argoops` | The **workspace root** (`$WS`) — `argoops-init.sh` resolves the state dir by walking UP from `$PWD` to the first `.claude/`; run it from `$WS` or the state lands in a nested `.claude/` (e.g. `platform/.claude/`) | `"$WS/.claude/scripts/argoops-*.sh"`, invoked with CWD=`$WS` |
| Gen-2 discovery pipelines | The **agent-kit checkout** (`$WS/<workspace>/<project>/agent-kit/`) — both `data/scripts/…` and the run's `.claude/notes/<name>s/<ID>/` state resolve there | `data/scripts/<name>-*` relative paths |
| Meta commands | The agent-kit checkout (they write `data/…`) | `data/scripts/…`, `data/references/…` |

Outcome-log emits (all families): invoke with `python3` (never `bash` — a `.py` under bash parses
the docstring as shell and silently no-ops behind `|| true`), and pin the corpus to the single
per-machine dataset with the purpose-built env override:
`PIPELINE_OUTCOME_LOG="$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl"`. Record schema:
`pipeline-outcome-log-schema.md`.

---

## 7. Why `/capability-scout` has its own synthesizer/prioritizer (and the other five don't)

Five of the six Gen-2 pipelines dispatch the **generic** `pipeline-synthesizer` /
`pipeline-prioritizer` agents, parameterized per-pipeline via `{PHASE2_REF}` / `{PHASE4_REF}`
(get_reference keys) and `{EXTRA_REFS}`. `/capability-scout` — the FIRST Workflow port
(2026-06-12) — still uses its own bespoke `capability-scout-synthesizer` /
`capability-scout-prioritizer`.

**Rationale (inference — the git history is consistent with this but no ADR states it):** the
generic parameterized agents were extracted AFTER capability-scout proved the port shape; the
later five ports adopted the generic pair, and capability-scout was never retrofitted because its
bespoke agents work and embed the capability-specific protocol directly rather than loading it via
`{PHASE2_REF}`. Functionally the two arrangements are equivalent (same tool caps: Read / Write /
get_reference; same model classes). If you touch capability-scout's workflow, migrating it to the
generic pair is a sanctioned cleanup — not required.

---

## 8. Quick self-check before running any pipeline

1. Which generation is this command? (§1 table — the command body's Phase-summary table also says.)
2. Is my CWD right for this family? (§6.)
3. Gen-2: is the Workflow tool available? If not, apply §3 — do not improvise re-inlining.
4. Am I about to pass `isolation: worktree`? Check §4 — most pipeline agents must NOT get it.
5. Fresh agent files this session? §5 — restart or use the general-purpose fallback.
6. `.py` script? `python3`, never `bash`.
7. Milestone run? Report code, publication, apply, and verification separately;
   confirm every external action came from a deterministic preview and exact
   human authorization.
