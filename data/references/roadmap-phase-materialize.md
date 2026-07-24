---
type: roadmap
status: active
tags:
  - type/roadmap
  - status/active
---
# Phase 4 — MATERIALIZE

**Goal:** finalize the local roadmap doc, lint it, and (if `--gitlab` was passed) gate-then-create GitLab artifacts. Always offer to kick off `/milestone-pipeline` for the first Now-lane milestone.

## Always: finalize the local roadmap doc

The roadmap doc at `<repo-root>/plans/<slug>-roadmap.md` was scaffolded in Step 0 and populated section-by-section across Phase 1–3. Now:

1. **Cross-references** — append a `## Cross-references` section listing:
   - Every milestone ID with its `/milestone-pipeline <id>` invocation.
   - Every spike with its decision-doc path.
   - Any related `plans/*` documents (precursor designs, retrospectives).
   - GitLab issues if any are linked (added later if `--gitlab`).
   - Confluence pages referenced.
   - Memory entries cited.

2. **Lint** — run the validator:

   ```bash
   python3 "$WS/.claude/scripts/roadmap-validate.py" <repo-root>/plans/<slug>-roadmap.md
   ```

   Exit 0 if all checks pass:
   - All required sections present (`## Goal`, `## Epics`, `## Roadmap — Now / Next / Later`, `## Cross-references`).
   - MoSCoW Must cap (≤ 60% of epics).
   - All Now-lane milestones have IDs of the form `<slug>-m<N>`.
   - No `[MUST]` assumptions remain unvalidated.
   - Spike lane is present (even if "all assumptions validated").
   - Won't list is present.
   - Every Now-lane story has Given/When/Then AC.

   Exit 1 with a list of failures if any check fails. **Fix and re-lint before proceeding.** If a failure is intentional (e.g. user explicitly accepted a Must-cap violation), document the rationale in the doc and use `--allow <check-name>` to suppress.

## Always: materialize the milestone register (`.claude/notes/roadmaps/<slug>/milestones.json`)

The register is the machine-readable twin of the Now lane — a local, regulated,
GitLab-issue-like object per milestone. **LOCAL-ONLY, never committed** (gitignored
`.claude/notes/` tier; policy 2026-07-09). `/milestone-pipeline` gates on it (dependency
enforcement at init) and updates it (status at completion); `pipeline-reconcile.py`
cross-checks it against the doc, state files, and critiques. Full schema + single-writer
rules: `roadmap-milestones-schema.md` (same references dir).

**Flow: the materializer agent authors a STRUCTURAL DRAFT (`<register>.incoming`);
`roadmap-milestones-merge.py` — the only sanctioned materialize writer — merges it into
the live register, preserving `status`/`run`/`history`/`gitlab` by id and REFUSING to
drop non-pending milestones (exit 3 → gate to the user).**

**Field mapping (doc → draft) — this table is canonical:**

| Register field | Derived from |
|---|---|
| `id`, `title` | `#### M<N>: <title> — milestone ID \`<slug>-mN\`` heading (Now lane only in v1) |
| `epic` | The milestone's `**Source epic:**` line |
| `lane` | `"now"` (v1 emits Now-lane only; Next/Later epics have no milestone ids yet) |
| `status` | `"pending"` in every draft — the MERGE preserves live status (never the agent) |
| `depends_on` | Source epic's `**Depends on:**` epics, mapped to those epics' Now-lane milestone ids; epics without a Now-lane milestone are skipped (a Now-milestone depending on a Next/Later epic is a sequencing bug — fix the lanes instead) |
| `tags` | `moscow/<must\|should\|could\|wont>` from the `**MoSCoW:**` line (exactly one) |
| `rice` | The RICE number, or `null` when "not scored" |
| `specialist` | The milestone's `**Specialist:**` line |
| `repos` | The ONE platform-relative repo path this milestone commits to — the roadmap's home repo by default. Never emit more than one: a `>1` entry is refused at fresh init (`init-state.sh` exit 6). Work spanning repos decomposes into one milestone per repo, chained with `depends_on` (dependency first). See `data/commands/milestone-pipeline.md` § Multi-repo |
| `external_writes` | Dedup of the stories' `External writes required:` lines |
| `gitlab`, `run`, `history` | All-null / empty skeletons in every draft; `gitlab.*` is written back by the orchestrator after gated issue creation; `run`/`history` belong to `roadmap-milestones-status.py` |

Merge + validate (both deterministic — non-zero blocks Phase 4):

```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
python3 "$WS/.claude/scripts/roadmap-milestones-merge.py" "<register>" "<register>.incoming"
python3 "$WS/.claude/scripts/roadmap-milestones-validate.py" "<register>"
```

**Re-materialize discipline:** structure (titles, deps, tags, epics) is re-derived from
the doc; execution state is preserved by the merge. A draft that drops an
`in_progress`/`complete`/`cancelled` milestone refuses to merge — surface it, don't
silently drop the object.

## Optional: GitLab artifacts (`--gitlab` flag — GATED)

> **THE MATERIALIZER SUB-AGENT DOES NOT EXECUTE THE STEPS BELOW.** Its job is to
> DRAFT issue bodies to local files under `{DRAFTS_DIR}/`. The orchestrator (main
> session) reads the drafts, presents them to the user for explicit per-event
> authorization per workspace CLAUDE.md "External System Write Policy", and
> THEN dispatches `/issue-create`. The materializer's scope-bounds block
> explicitly forbids GitLab MCP write tools, `gh`/`glab` CLIs, and `/issue-create`
> dispatch — this section describes the orchestrator-side flow that follows the
> materializer's draft phase.

This is an external write. Per workspace CLAUDE.md, every external write requires explicit user confirmation. Batch presentation is required.

### Step 1 — Construct epic issue bodies

For each Now-lane epic, fill in `data/references/roadmap-template-epic-issue.md` with:
- **Title** — `<Epic title from Phase 2>` (action verb, no conventional commit prefix).
- **Story** section — derived from Phase 1 Objective + the epic's role.
- **Guidance** section — the epic's specialist agent + the milestone ID.
- **Needs** section — dependencies on other epics or spikes.
- **Acceptance Criteria** section — the epic-level AC from Phase 2.
- **Labels** — `type::epic`, `DevStage::Backlog`, `Stage::Dev`, plus domain labels per `.claude/references/label-taxonomy.md`.

### Step 2 — Construct story issue bodies

For each Now-lane story, fill in `data/references/roadmap-template-story-issue.md` with:
- **Title** — story title (action verb).
- **Story** section — Given/When/Then AC from Phase 3.
- **Guidance** section — implementation hints, files likely touched.
- **Needs** section — dependencies (blocking stories, external setup, spike outputs).
- **Acceptance Criteria** section — the Given/When/Then AC plus the universal DoD reference.
- **Labels** — `DevStage::Backlog`, `Stage::Dev`, plus domain labels.
- **Epic link** — will be populated after the epic issue is created.

### Step 3 — Present batch for review

Show the user:
- Total count: N epic issues + M story issues + I iteration assignment (if `--iteration <id>`).
- Per-epic summary: title, label list, story count under it.
- Per-story summary: title, AC count, parent epic title.
- Repo path each issue will be created in.

Ask: **"Authorize creating these N+M GitLab issues? [yes / no / inspect <id>]"**

`inspect <id>` lets the user see the full body of a specific draft before authorizing.

### Step 4 — Execute (only after explicit `yes`)

For each epic in order:
1. Dispatch `/issue-create` with the epic body, `type::epic` label, and the rest.
2. Capture the returned issue iid.
3. For each story under this epic, dispatch `/issue-create` with the story body and `--epic-link <iid>` (creates issue link with `link_type: "relates_to"`).
4. If `--iteration <id>` was passed and the iteration is active, set the iteration on each created issue via `update_issue`.
5. Append the created issue iids to the roadmap doc's `## Cross-references` section AND write them into the register (`gitlab.epic_iid` / `gitlab.story_iids` per milestone in `.claude/notes/roadmaps/<slug>/milestones.json`) — the register is the local issue twin; iids keep the projection joined.

If any single `/issue-create` fails, STOP and report. Do not roll back created issues automatically — surface and let the user decide.

## Always: offer to kick off `/milestone-pipeline`

After Phase 4 completes (with or without GitLab artifacts), the natural next step is to start executing the first Now-lane milestone.

Print to the user:

```
Roadmap complete: <repo-root>/plans/<slug>-roadmap.md
First Now-lane milestone: <slug>-m1 (<title>)

To kick off execution:
  /milestone-pipeline <slug>-m1

Run this now? [yes / no / later]
```

- `yes` → invoke `/milestone-pipeline <slug>-m1` (the milestone-pipeline skill takes over from here; it will create its own state directory and start Phase 1 research).
- `no` / `later` → end Phase 4. Roadmap is durable; the user can resume execution any time with `/milestone-pipeline <slug>-mN`.

## Auto-advance vs gate (this phase)

| Condition | Action |
|---|---|
| Local roadmap-only flow (no `--gitlab`); validate-roadmap.py exits 0 | Auto-advance through writing + linting + offer milestone-pipeline kick-off |
| `--gitlab` flag was passed | GATE on the batch issue-creation review (always; per CLAUDE.md) |
| validate-roadmap.py reports a lint failure | GATE — present failure, ask whether to fix or accept with `--allow <check>` |
| User-explicit checkpoint request | GATE |
| `/milestone-pipeline` kick-off | ALWAYS confirm; never auto-invoke |

## Hard rules

- **Never push, create issues, assign iterations, or invoke `/milestone-pipeline` without explicit user authorization.** Per workspace CLAUDE.md.
- **Never edit `deploy/argocd-config-*`** — CI-generated.
- **Issue titles use action verbs**, no conventional commit prefixes.
- **Epic issues get `type::epic` label** (workspace convention — there's no native GitLab Epic API).
- **Story-to-epic linking** uses `mcp__GitLab__create_issue_link` with `link_type: "relates_to"`.
- **DevStage starts at `Backlog`**; `/issue-advance` moves stories forward as work progresses.
- **Iteration assignment requires the iteration to be active.** If `--iteration <id>` is passed but the iteration is closed/future, surface and ask.
- **Cross-references section is non-negotiable.** Without it, the roadmap can't be resumed cleanly weeks later.

## Resumability

Re-invoking `/roadmap <slug>` (or `/roadmap` after `cd` into the repo) on a partially-materialized roadmap:

1. Reads the existing `plans/<slug>-roadmap.md`.
2. Identifies the first unpopulated section (or finds the `<!-- TODO -->` marker the template uses).
3. Resumes at that phase.

If the doc has all four sections populated AND the user passed `--gitlab` on the resume, jump directly to Step 1 of the GitLab-artifacts flow above.
