---
description: Convert an existing skill at data/skills/<name>/SKILL.md into a slash-command + N-subagent + agent-memory pattern. Use when a skill has grown into a multi-phase orchestrator (e.g. has "Phase 1 / Phase 2 / Phase 3 / Phase 4" structure, dispatches sub-tasks, holds state across phases) and you want to refactor it to match the canonical /milestone-pipeline / /spike / /roadmap shape. Walks the 5-turn build pattern (Discovery → Parallel author → Adversary review → Rectify → Commit) end-to-end. Skip for simple skills with no multi-phase orchestration — those should stay as skills.
argument-hint: "<skill-name> [--skip-review] [--dry-run]"
type: command
status: active
tags:
  - type/command
  - status/active

---

# /skill-to-command — Skill→Command conversion orchestrator

Refactor an existing skill at `data/skills/<name>/SKILL.md` into the canonical slash-command + memory-bearing subagents pattern (the shape used by `/milestone-pipeline`, `/spike`, `/roadmap`).

**Arguments:** `$ARGUMENTS` — parse as `<skill-name> [--skip-review] [--dry-run]`

- `<skill-name>` — required. Must exist at `data/skills/<name>/SKILL.md`. If empty or absent, STOP and ask: "Which skill do you want to convert? (must exist at `data/skills/<name>/SKILL.md`)"
- `--skip-review` — skip Turn 3 adversarial review. Use only for trivial conversions where the author is confident. **Default is to always run the reviewer.**
- `--dry-run` — author all files but do NOT commit. Useful for sanity-checking the conversion before staging.

---

## When to use / When NOT to use

| Convert with `/skill-to-command` | Keep as a skill |
|---|---|
| Skill body has 3+ phases that each dispatch sub-work | Single-step skill (one prompt → one output) |
| Skill is >200 lines and growing | Skill is <100 lines and stable |
| Skill needs persistent memory across runs | Skill is purely stateless reference material |
| Skill currently uses TodoWrite to track multi-step state | Skill is a one-shot helper |
| Skill needs parallel agent dispatch in one of its phases | Skill is sequential by nature |

**Key signal:** if the skill body has `Phase 1 / Phase 2 / Phase 3 / Phase 4` sections, parallel agent dispatch, or `state.json`, it is a candidate. If it has none of those, leave it as a skill — the conversion overhead is not worth it.

---

## Inputs / outputs

| Input | Source |
|---|---|
| Skill body | `data/skills/<name>/SKILL.md` |
| Subdirectory references | `data/skills/<name>/references/*.md` (if present) |
| Subdirectory scripts | `data/skills/<name>/scripts/*` (if present) |
| Workspace CLAUDE.md | `data/claude-md/workspace-root.md` |
| Build pattern + 14 gotchas | `data/references/skill-to-command-conversion-prompt.md` |

| Output | Destination |
|---|---|
| Slash command orchestrator | `data/commands/<name>.md` |
| Phase subagents (N per phase) | `data/agents/<name>-<role>.md` |
| Migrated references (flat-named, `git mv`) | `data/references/<name>-<file>.md` |
| Migrated scripts (flat-named, `git mv`) | `data/scripts/<name>-<file>.{sh,py}` |
| Deprecated skill stub | `data/skills/<name>-deprecated/SKILL.md` |
| Build-time critique audit (UNTRACKED) | `.claude/notes/<name>-build-critique.md` |

---

## Phase summary (5-turn build pattern)

| Turn | Actor | Sub-agent name | Reads | Writes |
|---|---|---|---|---|
| 1. Discovery | Main session | (none — main thread) | existing skill + workspace CLAUDE.md | mental model of phases → subagent map |
| 2. Parallel authoring | `skill-to-command-author` × 2 (worktree isolation — legitimate: 2 parallel agents mutate tracked files) | conversion prompt §file templates | all new files on disjoint write surfaces |
| 3. Adversarial review | `skill-to-command-reviewer` × 1 (shared cwd — NO worktree: its critique lands in untracked `.claude/notes/`) | the new tree + 14-gotcha checklist | `.claude/notes/<name>-build-critique.md` |
| 4. Rectify | Main session | (none — main thread; preserves user-review surface) | critique findings | fixes for CRITICAL + HIGH + cheap MEDIUMs |
| 5. Commit | Main session | (none — main thread; external-write boundary) | none | single signed conventional commit |

The two named sub-agents (`skill-to-command-author`, `skill-to-command-reviewer`) live in `data/agents/`. Each has `memory: project` and accumulates lessons at `.claude/agent-memory/<agent-name>/lessons.md` across runs.

---

## Orchestrator steps

### Step 0 — Parse arguments and validate skill exists

```bash
# Parse from $ARGUMENTS — first non-flag token is the skill name.
SKILL_NAME="$(echo '$ARGUMENTS' | awk '{print $1}')"
SKIP_REVIEW=$(echo '$ARGUMENTS' | grep -q -- '--skip-review' && echo 1 || echo 0)
DRY_RUN=$(echo '$ARGUMENTS' | grep -q -- '--dry-run' && echo 1 || echo 0)

if [ -z "$SKILL_NAME" ]; then
  echo "STOP — which skill do you want to convert?" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
SKILL_PATH="$REPO_ROOT/data/skills/$SKILL_NAME/SKILL.md"

if [ ! -f "$SKILL_PATH" ]; then
  echo "STOP — skill not found at $SKILL_PATH" >&2
  exit 2
fi

# Confirm no name collision on the target command path.
if [ -f "$REPO_ROOT/data/commands/$SKILL_NAME.md" ]; then
  echo "STOP — data/commands/$SKILL_NAME.md already exists. Conversion would overwrite. Confirm with user." >&2
  exit 2
fi

# Prepare per-agent memory directories (lessons accumulate across runs).
mkdir -p ".claude/agent-memory/skill-to-command-author"
mkdir -p ".claude/agent-memory/skill-to-command-reviewer"
```

If validation passes, print a 3-line plan:
```
Converting skill: <name>
  source:  data/skills/<name>/SKILL.md
  target:  data/commands/<name>.md + data/agents/<name>-*.md + migrated refs/scripts
  dry-run: <yes|no>
```

### Step 1 — Turn 1: Discovery (main session)

**Goal:** Map the skill's existing phases to subagent roles. Decide write surfaces. Identify references/scripts to migrate.

Read in this order (in the main session, NOT a subagent — synthesis needs everything in working memory):

1. `data/skills/<name>/SKILL.md` end-to-end.
2. `data/skills/<name>/references/*.md` if present (the existing reference set; most will be `git mv`d, not rewritten).
3. `data/skills/<name>/scripts/*` if present.
4. `data/claude-md/workspace-root.md` for the External System Write Policy + git/commit conventions.
5. `data/references/skill-to-command-conversion-prompt.md` (the canonical build pattern + 14 critical gotchas — this is the load-bearing reference for the rest of the conversion).

Produce a discovery summary (write to working memory; you'll feed it to Turn 2):

- **Phases identified** (e.g. "Phase 1 REFINE → roadmap-refiner; Phase 2 DECOMPOSE → roadmap-decomposer; ...")
- **Write surface per agent** (which files each subagent owns — must be DISJOINT for parallel authoring)
- **Scripts to migrate** (list with old path → new flat-named path)
- **References to migrate** (list with old path → new flat-named path)
- **External-write boundary** (which agent's scope-bounds must explicitly forbid which writes — e.g. the materializer must not call any GitLab MCP write tool)
- **Memory hooks** (what `.claude/agent-memory/<name>-<role>/lessons.md` should accumulate)

### Step 2 — Turn 2: Parallel authoring (2 named sub-agents in ONE assistant turn)

**Goal:** Fan out construction. Two agents in ONE turn = ~2× speedup AND diversity.

Critical: send **both Agent calls in a single assistant message**. Sequential dispatch defeats the parallelism and is a documented gotcha.

Use `subagent_type: skill-to-command-author` on **both** Agent calls (NOT `general-purpose` — the named agent at `data/agents/skill-to-command-author.md` has the right scope-bounds, memory hooks, and untrusted-content-policy baked in). Use `isolation: worktree` on each so they can't trample each other's edits — this is the legitimate worktree case (two PARALLEL agents mutating TRACKED files in the same repo; resolved doctrine: `data/references/pipeline-pattern-v2.md` §4). If the harness doesn't support the parameter, the disjoint write surfaces make shared-cwd safe too — omit `isolation` rather than improvising.

The two invocations have DISJOINT write surfaces, encoded in the brief via the `{WRITE_SURFACE}` variable:

- **Agent A invocation** — `{WRITE_SURFACE} = "A"`:
  - Writes `data/commands/<name>.md`
  - Writes `data/agents/<name>-<role>.md` × N
  - Runs `git mv data/skills/<name>/references/<x>.md data/references/<name>-<x>.md` for each reference
  - Updates the new agent bodies to point at the new flat-named reference paths

- **Agent B invocation** — `{WRITE_SURFACE} = "B"`:
  - Runs `git mv data/skills/<name>/scripts/<x>.{sh,py} data/scripts/<name>-<x>.{sh,py}` for each script
  - Runs `git rm data/skills/<name>/SKILL.md` (the old orchestrator body)
  - Writes `data/skills/<name>-deprecated/SKILL.md` with `allowed-tools: Read` and a redirect message
  - If workspace CLAUDE.md (`data/claude-md/workspace-root.md`) needs an update to register the new command, makes the edit AND its byte-identical copy at the active workspace path

Each Agent call's prompt should be a 4-field brief substituted into the agent's input contract:
- `{SKILL_NAME}` — the skill being converted
- `{WRITE_SURFACE}` — `A` or `B`
- `{DISCOVERY_SUMMARY}` — the Turn 1 output (phases identified, scripts/refs to migrate with old→new paths, external-write boundary notes)
- `{ID}` — same as `{SKILL_NAME}` (for compatibility with the canonical agent-brief shape)

The detailed Agent A / Agent B brief contents are at `data/references/skill-to-command-conversion-prompt.md` §"Turn 2 — Parallel authoring".

After both return, run a quick sanity check:
```bash
test -f "data/commands/$SKILL_NAME.md" || { echo "Agent A failed to write command body"; exit 1; }
test -f "data/skills/$SKILL_NAME-deprecated/SKILL.md" || { echo "Agent B failed to write deprecated stub"; exit 1; }
ls "data/agents/$SKILL_NAME"-*.md | wc -l   # Expect N (the per-phase count from Turn 1)
git status --short                          # Should show A/R/?? entries on the expected surfaces
```

### Step 3 — Turn 3: Adversarial review (1 named sub-agent)

**Goal:** Walk the 14 critical gotchas against the new tree. Emit a DO-NOT-SHIP / SHIP verdict with severity-tagged findings.

Skip this turn if `--skip-review` was passed. (Strongly discouraged — every prior conversion turned up at least 1 CRITICAL.)

Dispatch ONE Agent with `subagent_type: skill-to-command-reviewer` (NOT `general-purpose`), **no `isolation` parameter** — the reviewer writes its critique to untracked `.claude/notes/<skill-name>-build-critique.md`, which a worktree would strand (doctrine: pipeline-pattern-v2.md §4). The named agent at `data/agents/skill-to-command-reviewer.md` has the 14-gotcha checklist embedded in its body and uses the conversion-prompt runbook for calibration.

Brief substitution:
- `{SKILL_NAME}` — the skill being converted
- `{CRITIQUE_PATH}` — `.claude/notes/<skill-name>-build-critique.md`
- `{TURN_2_SUMMARIES}` — JSON of the `files_written` / `files_renamed` / `files_deleted` returned by the two Turn 2 author invocations

Severity rubric:
- **CRITICAL** — the conversion is broken at first invocation (e.g. C1 = old SKILL.md not deleted, name-collides with new command; C2 = `bash` invoking `.py` files).
- **HIGH** — works on the happy path but fails on a realistic edge case (e.g. H1 = stdin-vs-argv mismatch; H2 = silent empty-fallback on REPO_ROOT).
- **MEDIUM** — works correctly but is fragile or surprising.
- **LOW** — cosmetic / nice-to-have.

The reviewer writes to `.claude/notes/<name>-build-critique.md` (UNTRACKED — build-time audit artifact, intentionally not committed).

### Step 4 — Turn 4: Rectification (main session)

**Goal:** Fix every CRITICAL + HIGH. Fix cheap MEDIUMs (≤30 LOC, single file). Record LOW deferrals in the commit body.

Run in the **main session**, NOT a subagent — rectifier needs full repo access AND the ability to pause at the external-write boundary.

For each finding:

1. **Re-verify** against the current code (read cited file:line ±30 lines). The Turn 2 agents may have already fixed something the reviewer flagged from a stale read. Note any invalidations.
2. **Fix** the confirmed CRITICAL + HIGH + cheap MEDIUM findings.
3. **Smoke-test** as appropriate:
   - `bash -n` on any modified shell scripts
   - `python3 -m py_compile` on any modified Python scripts
   - End-to-end test of script CLI shapes (stdin/stdout flows, exit codes, INITIALIZED vs RESUMING output formats)
   - Byte-identity diff for any workspace CLAUDE.md changes

Cap: 3 iterations per finding, 3 outer iterations on the smoke-test loop. If you hit the cap, surface to the user.

### Step 5 — Turn 5: Single signed conventional commit + offer push

Stage by **explicit path** (never `git add .` or `git add -A`).

Commit subject: `refactor(command): convert /<skill-name> skill to slash-command + N subagents` (must match `^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}` — keep ≤ 50 chars).

Use macOS GPG signing:
```bash
git -c gpg.program=/opt/homebrew/bin/gpg commit -F /tmp/<name>-commit.txt
```

Commit body should include:
- What's new (per-file inventory)
- What moved (`git mv` log, history preserved)
- Architecture table (phase ↔ agent ↔ writes)
- How it was built (5-turn pattern, agent IDs)
- Critique findings closed (C1, H1, …) with one-line rationale each
- Deferred (LOW) with rationale
- Smoke tests (commands + expected output)

If `--dry-run`, stop here. Otherwise:

**STOP and ask for push authorization** per workspace CLAUDE.md "External System Write Policy". Sample prompt:

> Conversion complete. Pending external write: `git push origin HEAD:main` to land commit `<sha>`. Authorize?

After explicit user authorization, push:
```bash
git push origin HEAD:main
```

---

## Hard rules

- **NEVER skip the discovery turn** (Turn 1). Mapping phases to agents wrong here cascades into rework at every subsequent turn.
- **PARALLEL agent dispatch in ONE turn** (Turn 2). Sequential dispatch is a documented gotcha and the most common conversion failure mode.
- **DISJOINT write surfaces** between Agent A and Agent B. They cannot both write `data/commands/<name>.md` — that's a guaranteed race.
- **FLAT NAMING** in `data/references/` and `data/scripts/` — `<name>-<file>.md`, not subdirectories. The MCP discoverer is non-recursive (`readdir()` only).
- **GIT MV, NOT COPY**, when migrating from `data/skills/<name>/references/<x>.md` → `data/references/<name>-<x>.md`. Preserves history.
- **DELETE the old `data/skills/<name>/SKILL.md`** (via `git rm`) — name collision with the new `/<name>` slash command is a CRITICAL gotcha (the C1 finding in every prior conversion).
- **DEPRECATED stub path is `data/skills/<name>-deprecated/SKILL.md`** (with the `-deprecated` suffix), NOT `data/skills/<name>/SKILL.md`. The suffix avoids resolution-order ambiguity with the new command.
- **DO NOT call `bash` on `.py` scripts** — bash cannot execute Python (shebang is a bash comment). Use `python3` invocations. (C2 gotcha from every prior conversion.)
- **STDIN over inline CLI argv** for scoring/scoring-style scripts that accept JSON. JSON-on-stdin generalizes; inline argv breaks at shell-escape edge cases. (C3/H1 gotchas.)
- **MEMORY hooks are `.claude/agent-memory/<name>-<role>/lessons.md`** (gitignored). Every subagent body must read at start + append at end.
- **EXTERNAL-WRITE BOUNDARY is load-bearing** — the materializer/issue-creator subagent's `<scope-bounds>` must explicitly forbid every GitLab MCP write tool, every `gh`/`glab` CLI, and `/issue-create` dispatch. External writes go through the orchestrator only.
- **NEVER auto-push.** Always ask for explicit push authorization per workspace CLAUDE.md, regardless of permission mode.

---

## Common rationalizations (anti-pattern guard)

| Tempting belief | Reality |
|---|---|
| "I'll skip Turn 1 — I already know what the skill does." | Discovery is where you map phases to agents AND write surfaces. Skipping it is the source of every "two agents wrote the same file" race condition. |
| "Sequential Turn-2 dispatch is fine, I want to read Agent A's output before launching B." | Sequential doubles wall-clock AND defeats the diversity benefit. Brief both agents from the Turn-1 summary; let them work in parallel; review both outputs at the same time. |
| "The reviewer always nitpicks — I'll skip Turn 3." | Every prior conversion turned up ≥ 1 CRITICAL the author missed. Reviewer cost is 1 sonnet agent ≈ 5 min. ROI is enormous. |
| "I'll fix the critique findings by amending the Turn-2 commits." | Don't. Single signed commit at Turn 5. Amending the agent commits muddies the audit trail. |
| "The deprecated stub doesn't matter — no one will hit the old skill name." | Some users have stale skill caches, symlinks, or muscle memory. The stub costs nothing and prevents silent confusion. |
| "I'll push at Turn 5 since I already have permission." | NO. Per workspace CLAUDE.md, every external write needs explicit per-event authorization. Conversion authorization ≠ push authorization. |

---

## Don'ts

- **Don't create a `SKILL.md` for the new orchestrator.** Skills are knowledge; slash commands are orchestration. The conversion's whole point is to move OUT of the skill mechanism.
- **Don't keep the old `data/skills/<name>/SKILL.md` "just in case".** The C1 gotcha. `git rm` it; the `<name>-deprecated/` stub takes over.
- **Don't bypass the discovery turn.** Even on a "simple" skill, Turn 1 surfaces hidden dependencies (cross-references between phase files, scripts with non-obvious CLI shapes).
- **Don't fan out > 2 agents at Turn 2.** Beyond 2 the write-surface partitioning gets fragile and the review cost balloons. If you genuinely need more, do two Turn-2 rounds.
- **Don't let the rectifier (Turn 4) run as a subagent.** Rectifier needs the user-review surface (Turn 5 stop) and full repo access.
- **Don't push without explicit authorization** (the External System Write Policy).
- **Don't auto-invoke the new `/<name>` command at end of build.** Building and running are separate concerns; the user invokes when ready.

---

## Smoke test (after conversion, before commit)

```bash
# 1. The new command exists
test -f "data/commands/$SKILL_NAME.md"

# 2. The deprecated stub exists at the -deprecated path (NOT collision-prone path)
test -f "data/skills/$SKILL_NAME-deprecated/SKILL.md"
test ! -f "data/skills/$SKILL_NAME/SKILL.md"

# 3. The N expected subagents are present
ls "data/agents/$SKILL_NAME"-*.md

# 4. All migrated scripts pass syntax check
for f in data/scripts/$SKILL_NAME-*.sh; do bash -n "$f"; done
for f in data/scripts/$SKILL_NAME-*.py; do python3 -m py_compile "$f"; done

# 5. References are flat-named (no subdirs left under data/skills/<name>/)
test ! -d "data/skills/$SKILL_NAME/references"
test ! -d "data/skills/$SKILL_NAME/scripts"

# 6. Workspace CLAUDE.md byte-identity (if updated)
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
bash data/scripts/claude-md-copy-lint.sh || diff "$WS/CLAUDE.md" "data/claude-md/workspace-root.md"
```

All 6 checks must pass before staging the commit.

---

## State / resumability

This command does NOT use a state.json — the file system IS the state. Each turn's output is durable (committed agents/refs/scripts, untracked critique notes). Re-invoking `/skill-to-command <skill-name>` on a partially-converted skill detects existing target files and either:
- All target files exist → assume Turn 2 done, jump to Turn 3.
- Target files missing but old skill still has SKILL.md → assume nothing started; run from Turn 1.

The build-time critique at `.claude/notes/<name>-build-critique.md` is UNTRACKED (by design). After successful conversion + push, you may delete it. Before that, it's the audit trail for the rectification.

---

## Sub-agent memory

Both `skill-to-command-author` and `skill-to-command-reviewer` have `memory: project`. They accumulate lessons across runs at:

- `.claude/agent-memory/skill-to-command-author/lessons.md` — reusable scaffolding patterns, sed-pattern surprises, phase-naming collisions.
- `.claude/agent-memory/skill-to-command-reviewer/lessons.md` — calibration data (severity-rubric drift), gotcha-frequency surprises.

Do NOT clear or overwrite these directories. Each agent reads its own lessons at Step 0 and appends a one-line entry at the final step.

Per the workspace memory model (see `data/references/agent-memory.md`), the `memory: project` frontmatter is a future-CLI hint; the LOAD-BEARING mechanism is the explicit Read + Append in the agent body — both are already wired in.

## Reference (lazy-loaded)

The full build pattern, the 14 critical gotchas catalog, the verbatim Agent A / Agent B / Reviewer brief contents, and the smoke-test commands are at:

**`data/references/skill-to-command-conversion-prompt.md`**

Read it once at Turn 1 start. The orchestrator body above is the executive summary; the reference is the detailed runbook.

---

## How to invoke (examples)

```text
/skill-to-command my-pipeline
/skill-to-command tenant-onboarding --skip-review
/skill-to-command auth-chain-debug --dry-run
```

After conversion completes and the push lands:

```text
/<name> <id>   # invoke the new slash command
```

The conversion is one-way; the deprecated stub at `data/skills/<name>-deprecated/SKILL.md` redirects any leftover skill resolution to the slash command for ~30 days, after which it can be removed.
