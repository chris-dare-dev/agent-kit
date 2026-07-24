---
description: Build a structured technical roadmap (Goal → Epics → Now/Next/Later milestones → Stories) from a problem statement or the current conversation. Use when the user invokes `/roadmap <topic>`, says "plan this project", "build a roadmap for", "decompose this into milestones", "what's the path to ship X", or has been discussing a problem and now wants a structured plan. Output is a local `plans/<slug>-roadmap.md` and (optional) gated GitLab artifacts. Each Now-lane milestone is sized so `/milestone-pipeline <id>` can execute it.
argument-hint: "<slug-or-topic> [--gitlab] [--iteration <id>] [--resume]"
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active

---

# Roadmap Orchestrator

You are the orchestrator. The main session IS the pipeline. Subagents cannot spawn subagents — every dispatch comes from here.

A four-phase **gated** roadmap builder. Auto-advances within a phase; gates between phases ONLY when there is an architecturally divergent fork the user must resolve. Always gates on external writes per workspace CLAUDE.md.

```
Phase 1 REFINE      → roadmap-refiner     → ## Goal section in plans/<slug>-roadmap.md
Phase 2 DECOMPOSE   → roadmap-decomposer  → ## Epics section
Phase 3 SEQUENCE    → roadmap-sequencer   → ## Roadmap — Now / Next / Later section
Phase 4 MATERIALIZE → roadmap-materializer → ## Cross-references + optional GitLab drafts
```

## Parsing $ARGUMENTS

- `<slug-or-topic>` — everything before the first `--`. Used verbatim as the brief if short and specific (≤40 chars, kebab-case → slug). If longer prose, treat as the brief and derive the slug from it (kebab-case, ≤40 chars).
- `--gitlab` — Phase 4 materializer drafts GitLab issue bodies. The orchestrator presents the drafts and waits for explicit user authorization before calling `/issue-create`.
- `--iteration <id>` — if `--gitlab` and issues are created, assign them to this iteration id.
- `--resume` — skip the init check; read the existing roadmap doc and jump to the first unpopulated section.

If `$ARGUMENTS` is empty, check whether the current conversation contains a problem being discussed. If yes, derive the brief from the conversation (see Step 0). If no problem context exists, STOP and ask: "What would you like to roadmap?"

## When to invoke / When NOT to invoke

**Invoke for:**
- Multi-epic plans (≥ 2 epics spanning ≥ 2 weeks of work)
- When the user says "plan this project", "build a roadmap for X", "decompose this into milestones", "what's the path to ship Y", "help me sequence this work"
- Mid-conversation: the user has been discussing a problem and now wants a structured plan

**Do NOT invoke for:**
- **Single milestone known and well-scoped** → go straight to `/milestone-pipeline <id>`
- **Single-issue ticket creation** → use `/issue-create`
- **Status or promotion check** → use `/env-promotion-diff`, `/argocd-status`
- **Re-plan of an already-shipped milestone** → write `plans/<slug>-retrospective.md` directly; `/roadmap` is forward-looking
- **No problem statement at all** → STOP and ask what the user wants to roadmap

---

## Step 0 — Establish brief + slug + scaffold

**If invoked with `/roadmap <topic>`:** use `<topic>` as the brief.

**If invoked mid-conversation without a topic:** summarize the conversation as the brief in 2–4 sentences. Read it back: "Use this as the brief? [yes/no]" — quick confirm, not a full gate. Adjust on "no" then proceed.

**Runtime contract (CWD + paths — full model: `data/references/runtime-contract.md`):**
- **Required CWD: inside the TARGET repo** — the clone whose `plans/<slug>-roadmap.md` will hold
  the roadmap. The workspace root, `GitLab/`, and `platform/` are plain directories, NOT git
  repos — `git rev-parse` FAILS there (that is why the fallback below must never silently walk up).
- Scripts are invoked via the workspace symlink `"$WS/.claude/scripts/roadmap-<file>"`
  (→ `data/scripts/`, flat naming), which resolves from any CWD. `.py` scripts always run under
  `python3`, never `bash`.

Generate a slug from the brief title (kebab-case, ≤40 chars). Determine the target repo (mirror the 4-source fallback in roadmap-init.sh — match the order, STOP on empty rather than passing an empty string downstream):
```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
[ -d "$WS" ] || { echo "Set PERSONAL_WORKSPACE_ROOT to your the workspace" >&2; exit 1; }

# 1. $REPO_ROOT env var (set by caller)
# 2. $PLATFORM_ROOT env var
# 3. git rev-parse --show-toplevel from CWD
# 4. otherwise STOP with a helpful error — do NOT walk up from $0
REPO_ROOT="${REPO_ROOT:-}"
[[ -z "$REPO_ROOT" && -n "${PLATFORM_ROOT:-}" && -d "$PLATFORM_ROOT" ]] && REPO_ROOT="$PLATFORM_ROOT"
[[ -z "$REPO_ROOT" ]] && REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT" ]]; then
  echo "ERROR: cannot determine repo root. Run /roadmap from inside the target git clone, or set REPO_ROOT or PLATFORM_ROOT." >&2
  exit 2
fi
```

Scaffold (idempotent — if file exists, prints RESUMING + exits 0):
```bash
bash "$WS/.claude/scripts/roadmap-init.sh" "<slug>" "<brief>" --repo-root "$REPO_ROOT"
```

`roadmap-init.sh` prints one of:
- `INITIALIZED: <path>` — fresh roadmap
- `RESUMING phase=<refine|decompose|sequence|materialize>: <path>` — file exists; jump to first unpopulated section

On `RESUMING` (or when `--resume` is passed): read the roadmap doc, identify which `## ` sections are already populated, and jump directly to the Step for the first unpopulated section. Do NOT re-run completed phases.

Set:
```
SLUG=<derived slug>
BRIEF=<verbatim brief text>
ROADMAP_PATH=<repo-root>/plans/<slug>-roadmap.md
GITLAB_MODE="off"           # default
[[ "$ARGUMENTS" =~ --gitlab ]] && GITLAB_MODE="draft-only"
DRAFTS_DIR="<repo-root>/.claude/notes/roadmaps/<slug>/drafts"
ITERATION_ID=""
[[ "$ARGUMENTS" =~ --iteration[[:space:]]+([0-9]+) ]] && ITERATION_ID="${BASH_REMATCH[1]}"
# Note: --iteration without --gitlab has no effect — iteration assignment requires
# issue creation, which only happens in GITLAB_MODE=draft-only after user auth.
```

---

## Step 1 — Dispatch roadmap-refiner

Dispatch ONE `roadmap-refiner` subagent with this header prepended to the dispatch prompt:

```
SLUG={SLUG}
BRIEF={BRIEF}
ROADMAP_PATH={ROADMAP_PATH}
```

**Wait for return.** Route on `status`:

| status | action |
|---|---|
| `complete` | Proceed to Step 2 |
| `gate-required` | Present the gate question from `summary` line 2 to the user. On user resolution, re-dispatch with the answer appended to BRIEF (prefix: "User resolved: <answer>."). Proceed to Step 2 after the re-dispatch returns `complete`. |
| `aborted-scope` | STOP — surface `summary` to the user. The brief has no problem statement (only solution language). Ask for a problem restatement. |

Do NOT echo the roadmap doc section into orchestrator context. Read the file only if you need to present a gate question.

---

## Step 2 — Dispatch roadmap-decomposer

Dispatch ONE `roadmap-decomposer` subagent:

```
SLUG={SLUG}
ROADMAP_PATH={ROADMAP_PATH}
```

**Wait for return.** Route on `status`:

| status | action |
|---|---|
| `complete` | Proceed to Step 3 |
| `gate-required` | Present the decomposition-cut choice from `summary` to the user (the agent will have described 2 credible cuts). On user pick, re-dispatch with `USER_DECOMP_CHOICE=<user's choice>` added to the header. Proceed to Step 3 after `complete`. |
| `aborted-scope` | STOP — surface `summary`. |

---

## Step 3 — Dispatch roadmap-sequencer

Dispatch ONE `roadmap-sequencer` subagent:

```
SLUG={SLUG}
ROADMAP_PATH={ROADMAP_PATH}
```

The sequencer calls `"$WS/.claude/scripts/roadmap-score-moscow.py"` and `"$WS/.claude/scripts/roadmap-score-rice.py"` internally. It does NOT return these scripts' raw stdout — only the summarized result.

**Wait for return.** Route on `status`:

| status | action |
|---|---|
| `complete` | Proceed to Step 4 |
| `gate-required` | The Must/Should cut-line is contested. `summary` line 2 lists the contested epics with RICE scores. Present both sets to the user and ask for a tie-break decision. Re-dispatch with `USER_MOSCOW_CHOICE=<choice>` added. Proceed to Step 4 after `complete`. |
| `aborted-scope` | STOP — surface `summary`. |

---

## Step 4 — Dispatch roadmap-materializer

Dispatch ONE `roadmap-materializer` subagent:

```
SLUG={SLUG}
ROADMAP_PATH={ROADMAP_PATH}
GITLAB_MODE={GITLAB_MODE}
DRAFTS_DIR={DRAFTS_DIR}
MILESTONES_PATH=<repo-root>/.claude/notes/roadmaps/{SLUG}/milestones.json
```

The materializer ALWAYS lints (`roadmap-validate.py`) and ALWAYS materializes the machine-readable milestone register at `MILESTONES_PATH` — authoring a structural draft that `roadmap-milestones-merge.py` deterministically merges (execution state preserved by id) and `roadmap-milestones-validate.py` validates (schema: `data/references/roadmap-milestones-schema.md`). The register is **LOCAL-ONLY — never committed** (it lives in the gitignored `.claude/notes/` tier per the 2026-07-09 policy); it is what `/milestone-pipeline` gates on (dependency enforcement) and updates (status) — the roadmap doc keeps the prose. If `GITLAB_MODE="draft-only"`, the materializer also writes draft issue bodies to `{DRAFTS_DIR}/epic-<n>.md` and `{DRAFTS_DIR}/story-<m>.md`.

**Wait for return.** Route on `status`:

| status | action |
|---|---|
| `complete` | See external-write boundary below |
| `gate-required` | The lint failed. `summary` line 2 contains the failure list. Present failures to the user, ask whether to fix or accept with `--allow <check>`. If fix: re-dispatch. If accept: re-dispatch with `ALLOW_CHECKS=<check-names>` added. |
| `aborted-scope` | STOP — surface `summary`. |

**External-write boundary (CRITICAL — load-bearing):**

If `GITLAB_MODE="draft-only"` and status is `complete`:

1. List the draft files the materializer wrote: `ls {DRAFTS_DIR}/`
2. Present: "The materializer has drafted N epic issue bodies and M story issue bodies in `{DRAFTS_DIR}/`. Authorize creating these N+M GitLab issues? [yes / no / inspect <filename>]"
3. `inspect <filename>` → read and display that draft file. Ask again.
4. **On `yes` only:** for each epic draft in order, dispatch `/issue-create` with the draft body (read the file, pass as body). Capture the returned iid. For each story draft under that epic, dispatch `/issue-create` with `--epic-link <iid>`. If `ITERATION_ID` is set, pass `--iteration {ITERATION_ID}`.
5. After all issues created, append the iids to the roadmap doc's `## Cross-references` section AND write them back into the register: set `gitlab.epic_iid` / `gitlab.story_iids` on each affected milestone in `<repo-root>/.claude/notes/roadmaps/{SLUG}/milestones.json` (orchestrator edit — the register is the local issue twin; iids keep the projection joined).
6. **On `no`:** skip issue creation. The drafts remain in `{DRAFTS_DIR}/` for manual review.

**NEVER auto-create GitLab issues.** Authorization to start `/roadmap` is NOT authorization to create GitLab issues. Per workspace CLAUDE.md "External System Write Policy".

**Capture the run outcome** (E1/KR1 outcome log — best-effort, local-only, never blocks) once the materializer returns `complete`:
```bash
# python3, never bash. PIPELINE_OUTCOME_LOG pins the record to the single per-machine corpus
# (convention: data/references/pipeline-pattern-v2.md §6).
PIPELINE_OUTCOME_LOG="$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl" \
python3 "$WS/.claude/scripts/pipeline-outcome-log.py" emit --pipeline roadmap --id "{SLUG}" \
  --field status=complete --field source_state_path="plans/{SLUG}-roadmap.md" || true
```

**Emit the finalized artifact set** (Phase 5 receipt — best-effort, local-only) after any
authorized GitLab iid projection has updated the roadmap/register:
```bash
if [ -f "$WS/scripts/artifact_skill_capture.py" ]; then
  python3 "$WS/scripts/artifact_skill_capture.py" emit \
    --workspace "$WS" --producer roadmap --run-id "{SLUG}" \
    --path "{ROADMAP_PATH}" --path "{MILESTONES_PATH}" --apply
fi
```
The receipt is append-only routing intent for the later incremental ingester. It writes to neither
Qdrant nor Graphiti, and Graphiti bulk ingestion remains disabled. A capture failure never rolls
back or rewrites the completed roadmap; surface `ingestion receipt: unavailable|failed` in the
completion summary.

**Always offer `/milestone-pipeline` kick-off:**

```
Roadmap complete: {ROADMAP_PATH}
Ingestion receipt: <created|idempotent|unavailable|failed> [<event id or receipt path>]
First Now-lane milestone: {SLUG}-m1 (<title from roadmap>)

To kick off execution:
  /milestone-pipeline {SLUG}-m1

Run this now? [yes / no / later]
```

- `yes` → invoke `/milestone-pipeline {SLUG}-m1` immediately.
- `no` / `later` → end. Roadmap is durable; the user can resume any time.

---

## File-presence state model

The roadmap doc IS the state. Resume by reading which `## ` sections are populated:

| Sections present in roadmap doc | First unpopulated section | Phase to run next |
|---|---|---|
| None (or only `## Brief`) | `## Goal` | Step 1 — refiner |
| `## Goal` populated | `## Epics` | Step 2 — decomposer |
| `## Goal` + `## Epics` populated | `## Roadmap — Now / Next / Later` | Step 3 — sequencer |
| All three above populated | `## Cross-references` | Step 4 — materializer |
| All four populated | — | Done; offer `--gitlab` or `/milestone-pipeline` |

"Populated" means the section has content beyond HTML comments and placeholder text.

---

## Anti-pattern guard (push back BEFORE Phase 4)

| Tempting belief | Reality |
|---|---|
| "I'll skip Phase 1 — the brief is clear enough." | If you can't restate the brief as a "How Might We" with explicit Won'ts in 90 seconds, the brief is NOT clear enough. The 5-min refine cost prevents an entire wrong-direction roadmap. |
| "I'll cut everything as Must so we don't lose anything." | Then MoSCoW is meaningless. Cap Musts ≤ 60%. The script enforces this. |
| "I'll fold the spike work into the epic itself." | Spikes are time-boxed with a decision-doc output; epics are time-boxed with a shipped-feature output. Mixing them costs you a clean stop-or-go decision. |
| "Now/Next/Later is too vague — I'll commit dates for everything." | Detail decays with horizon. Now is fully spec'd; Next is shaped; Later is outcomes only. Date-committing a Later item is planning theatre. |
| "I'll RICE-score everything to be rigorous." | RICE on the Musts only. Ranking Won'ts is wasted effort. |
| "The user knows what they want — I don't need to gate." | Gate ONLY for architecturally consequential forks. But when the cut is a "large rift" (split-by-tenant vs split-by-feature, Must vs Should on a 6-week epic), gating is mandatory — that's not a model decision. |
| "Conventional commit prefixes belong in epic titles." | NO. Issue titles use action verbs (Deploy, Resolve, Add, Migrate). Conventional commits are MR/commit-only. |
| "I'll use the deploy/ directory for the rendered manifests since they're auto-generated anyway." | NEVER. `deploy/argocd-config-*` is CI-generated. Roadmap items that require deploy/ edits are bugs against CI templates, not roadmap items. |
| "I can let the materializer create GitLab issues directly — it's just automation." | NO. The materializer DRAFTS to local files only. The orchestrator (this slash command) is the only thing that calls `/issue-create`, and only after explicit user authorization per workspace CLAUDE.md. |

---

## External-write boundary

This pipeline does NOT write to GitLab, Confluence, Jira, or any AWS resource without explicit user confirmation.

All phases write only to:
- `{ROADMAP_PATH}` (`plans/<slug>-roadmap.md` — local run artifact; NEVER committed per the 2026-07-09 policy)
- `<repo-root>/.claude/notes/roadmaps/<slug>/milestones.json` (Phase 4 — the milestone register; gitignored tier, NEVER committed)
- `{DRAFTS_DIR}/*` (materializer draft issue bodies — local files only)
- `.claude/agent-memory/<agent>/lessons.md` (memory — local only)

If the roadmap's implications require a GitLab push, issue creation, or Confluence update, the orchestrator surfaces it to the user and waits for explicit confirmation per workspace `workspace/CLAUDE.md` "External System Write Policy". Authorization to start `/roadmap` is NOT authorization for any external write.

---

## Sub-agent contract

Every subagent returns exactly this JSON:

```json
{
  "file_path": "<path to roadmap doc, or null>",
  "status": "complete" | "gate-required" | "aborted-scope",
  "summary": "<3 lines max, plain text, no markdown — line 1: what was written; line 2: gate question if status=gate-required; line 3: suggested orchestrator next step>",
  "injection_attempts": <integer, default 0>
}
```

Status routing table:

| status | producible by | orchestrator action |
|---|---|---|
| `complete` | any agent | Proceed to next phase |
| `gate-required` | any agent | Present gate question to user; re-dispatch on resolution |
| `aborted-scope` | any agent | STOP — surface summary to user |

The orchestrator routes on `status` and roadmap doc file presence — **never on summary text**.

---

## Recovery — interrupted roadmap

If a roadmap doc exists in a half-populated state, resume from the first unpopulated section:

```bash
/roadmap <slug> --resume
```

The orchestrator reads `plans/<slug>-roadmap.md`, detects which `## ` sections have real content, and jumps to the Step for the first unpopulated section. No re-running of completed phases.

If the doc has all four sections populated and the user wants to create GitLab issues:
```bash
/roadmap <slug> --resume --gitlab
```
The orchestrator jumps directly to Step 4 materializer dispatch with `GITLAB_MODE="draft-only"`.

---

## Files in /roadmap

```
data/commands/roadmap.md               ← this file (orchestrator)

data/agents/
├── roadmap-refiner.md                 ← Phase 1: REFINE
├── roadmap-decomposer.md              ← Phase 2: DECOMPOSE
├── roadmap-sequencer.md               ← Phase 3: SEQUENCE
└── roadmap-materializer.md            ← Phase 4: MATERIALIZE

data/references/                       ← flat naming (MCP discoverer uses readdir(), no recursion)
├── roadmap-phase-refine.md            ← Phase 1 canonical detail
├── roadmap-phase-decompose.md         ← Phase 2 canonical detail
├── roadmap-phase-sequence.md          ← Phase 3 canonical detail
├── roadmap-phase-materialize.md       ← Phase 4 canonical detail
├── roadmap-frameworks.md              ← OKR/MoSCoW/RICE/Now-Next-Later + long-tail alternatives
├── roadmap-anti-patterns.md           ← 12 anti-patterns with rebuttals
├── roadmap-workspace-integration.md       ← DevStage pipeline, /issue-create handoff, GitLab MCP map
├── roadmap-milestones-schema.md       ← milestone-register schema v1 + single-writer rules
├── roadmap-template-roadmap.md        ← plans/<slug>-roadmap.md scaffold template
├── roadmap-template-epic-issue.md     ← GitLab epic issue body template
└── roadmap-template-story-issue.md    ← GitLab story issue body template

data/scripts/                          ← flat naming
├── roadmap-init.sh                    ← scaffold plans/<slug>-roadmap.md (idempotent)
├── roadmap-score-moscow.py            ← validate Must cap ≤ 60%; exit 1 if violated
├── roadmap-score-rice.py              ← rank Musts by RICE score (stdout table)
├── roadmap-validate.py                ← lint required sections, milestone ID format, AC presence
├── roadmap-milestones-validate.py     ← structural lint of the register (schema/DAG/enums)
├── roadmap-milestones-merge.py        ← ONLY materialize writer (draft→register, state preserved)
├── roadmap-milestones-status.py       ← ONLY status writer + dependency gate
└── pipeline-reconcile.py              ← advisory drift check across register/doc/state/critiques

<repo-root>/plans/
└── <slug>-roadmap.md                  ← the durable output (LOCAL run artifact — never committed)

<repo-root>/.claude/notes/roadmaps/<slug>/
├── milestones.json                    ← machine-readable milestone register (gitignored tier)
└── drafts/                            ← GitLab issue-body drafts (only with --gitlab)
    ├── epic-1.md                      ← drafted GitLab issue body (materializer writes; user authorizes)
    ├── epic-2.md
    ├── story-1.md
    └── story-2.md
```
