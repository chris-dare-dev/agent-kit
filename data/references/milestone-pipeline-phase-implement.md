---
type: reference
project: milestone-pipeline
status: active
tags:
  - type/reference
  - project/milestone-pipeline
  - status/active
---
# Phase 2 — Implement

**Goal:** turn the merged research synthesis into committed code on `dev` (or a parallel-explorer worktree, or via a specialist agent), with the project's check command (Helm template, Pulumi preview, kubectl --dry-run, lint/test) green.

## Read first

The main session reads BOTH briefs end-to-end before deciding execution path. Do NOT read partial briefs. The "Recommended approach" sections are often complementary, not redundant — agreement is the strongest signal; disagreement is the most informative.

If the research briefs flagged an existing **specialist agent** under "Existing skills/agents that could implement this", that biases the path decision toward Specialist.

## Three execution paths

```
Does the merged plan obviously match a workspace specialist agent?
  (helm-apps for new charts; gitops for ApplicationSet/sync issues;
   service-mesh for Istio/cross-cluster; security for OAuth/Kyverno;
   tenant-onboarding for new-tenant rollouts; helm-migration for
   major chart bumps with breaking CR changes; ci-pipelines for
   pipeline template work; node-ops for ASG/capacity changes;
   release-manager for env promotion; observability-ops for
   cross-cluster Thanos/Kiali changes)
├── YES → Specialist. Dispatch ONE specialist agent (no worktree —
│         specialists work in main repo). Treat its return as the
│         implementation. Synthesize merged research brief + the
│         relevant specialist context into a single dispatch prompt.
└── NO  → Continue to Inline-vs-Delegated decision below.
```

Then for non-specialist work:

```
Is the merged plan ≤500 LOC across ≤5 files
AND no UI scaffolding required
AND no novel architectural component?
├── YES → Inline. Implement in main session.
│         (faster, less coordination, easier to course-correct mid-flight)
└── NO  → Delegated. Dispatch 1–2 implementers with isolation: worktree.
          (branch convention: {ID}-explorer-{a|b}; LOCAL-ONLY, never pushed —
           `feature/*` is forbidden by the user-level CLAUDE.md lock-in)
```

**User override:** if the user asked for a specific path explicitly ("delegate to gitops agent", "implement inline", "parallel explorer worktrees"), follow the user's instruction.

## Specialist path

Dispatch one workspace specialist agent (NOT `general-purpose`). Specialists already know their domain — pass them:
- The merged research synthesis (1 paragraph)
- The specific files they should touch (from the research brief's "Recommended approach")
- Expected publication and per-target operational actions. These inform the
  later release manifest and frozen operations plan; they are not authorization.

The specialist works in the main repo (no `isolation: worktree`) and returns when the work is committed locally on `dev`. Do not push or open MRs; publication is a later main-session phase after code-complete.

Record the choice:
```bash
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set 'implementation_path="specialist"'
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set 'implementation_specialist="service-mesh"'
```

## Inline path

1. Read both briefs.
2. Write a 5-bullet plan to `<repo-root>/.claude/notes/milestones/{ID}/artifacts/implementation-plan.md` (the Rectifier reads this in Phase 4 to confirm the implementation matched intent).
3. Implement. Commit small (≤200 LOC per commit when feasible). Conventional-commit subject: `{type}({ID}): {what}` — `type` ∈ `feat|fix|refactor|test|docs|perf|chore|ci`.
4. Run the project check (see "Validation per subsystem" below). Fix anything that breaks until green.
5. Record state and checkpoint.

## Delegated path

Use 1 OR 2 `milestone-implementer` subagents. The agent body at `.claude/agents/milestone-implementer.md` is self-contained — the orchestrator's dispatch prompt prepends substituted variables (ID, BRIEF_PATH, BRANCH_NAME, REPO_ROOT, WORKSPACE_ROOT). **Worktree isolation note:** the `Agent` tool's `isolation: worktree` parameter requires the caller's CWD to be inside a git repo — if not, the orchestrator must `cd` into the platform repo first, OR use explicit `git worktree add` in bash. Verify against your current harness before dispatching parallel explorers.

| When 2 implementers | When 1 implementer |
|---|---|
| The two research briefs disagreed on approach AND both approaches are credible | Briefs agreed; OR milestone is too small to justify duplicate work |
| User explicitly asked for parallel exploration | Default for the Delegated path |

Each implementer receives:
- Its assigned brief (agent-a → implementer-a, agent-b → implementer-b)
- The full Implementer prompt
- The branch name to use (`{ID}-explorer-{a|b}` for Delegated path — LOCAL-ONLY scratchpad worktree, never pushed. Publication is a reviewed commit pushed directly to the trunk `main`; create a feature branch/MR only when Chris explicitly requests a review gate)
- An instruction to run the project check before declaring done

When implementer(s) return:
- **Single implementer:** review the worktree commits; if green, rebase/synthesize them onto current `main`, then push the reviewed commit directly to `main` after authorization.
- **Two implementers:** read both worktrees end-to-end. Synthesize into one commit on current `main`. The synthesis itself is small (≤200 LOC of integration glue) and stays inline; the bulk of the code is one of the two worktrees rebased onto `main`. Delete the explorer branches after synthesis; push the reviewed commit directly to `main` after authorization.

Either way, the project check must pass on the exact commit before it is pushed to `main`.

## Validation per subsystem

**How to pick the row:** match the repo's lockfile / project marker. `uv.lock` → uv-Python; `bun.lockb` → bun-JS; `package-lock.json` → npm-JS; `go.mod` → Go; `pyproject.toml` without `uv.lock` → check the `[tool]` section for poetry/pip-tools and copy that tool's CI command. If still uncertain, read the repo's `.gitlab-ci.yml` `lint` / `test` job and copy its commands verbatim.

**Read-only commands.** Every cell uses `--check` / `-l` style read-only flags. They tell you "did I pass?" and are safe to copy into a CI-style verification gate. The corresponding *fix* commands (e.g. `ruff format <files>` without `--check`, `prettier --write <files>` instead of `--check`) live in the `milestone-implementer` agent body at `.claude/agents/milestone-implementer.md` and run *before* the validation, scoped to the touched files only — never repo-wide.

| Subsystem | Validation command |
|---|---|
| Helm chart | `helm template charts/<name> -f charts/<name>/values.yaml \| kubectl --dry-run=client apply -f -` |
| Helm chart with overlays | Same as above for each overlay |
| Pulumi (infra/) | `pulumi preview` from the stack dir; expected diff = your change only |
| ci-cd-templates/ | `mcp__GitLab__validate_project_ci_lint` against the consuming project |
| source/ Python app (uv) | `uv run pytest -v` AND `uv run ruff check .` AND `uv run ruff format --check .` (CI gates all three; running only the first two is what shipped 4 retro fix-up commits in admin-mcp-phase-2) |
| source/ Go app | `go test ./... && go vet ./... && gofmt -l . \| (! grep .)` |
| source/ TS/JS app (bun) | `bun run test` (or `bun run test:cost && bun run test:react`) AND `bun run lint` AND `bun run format:check` (CI gates `format:check`; `lint` alone is not enough — see admin-mcp-phase-2 prettier failures) |
| source/ TS/JS app (npm) | `npm test && npm run lint && npm run format:check` |
| Mixed | Run all relevant from above |

The Phase 3 critic uses validation pass/fail as one of its axes. Don't checkpoint Phase 2 with a failing validation.

## Conventional commit format (REQUIRED — repo lint enforces)

```
^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}
```

Examples:
```
feat(ISSUE-1234): add Kiali multi-cluster secret for tenant-acme
fix(ISSUE-1234): correct IRSA role naming in pkg/irsa
refactor(ISSUE-1234-rect): close C1, H2, M1 from adversary critique
ci(epic-coder-rollout): add deploy stage for tenant-example
```

macOS GPG signing:
```bash
git -c gpg.program=/opt/homebrew/bin/gpg commit -m "..."
```

## Branch strategy (REQUIRED — per CLAUDE.md)

> **Single-trunk (direct-main workflow locked 2026-07-17, Chris Dare).** Trunk = `main`,
> fleet-wide. Routine work commits and pushes directly to `main`; use a feature branch/MR only when
> Chris explicitly requests a review gate. `dev` is being retired (already DELETED on `ci-cd-templates`; still physically
> present on some repos pending gated deletion — no longer a commit target). Scope:
> `example-org/platform/*` (excludes the un-migrated `platformprime_*` group).

- Source + chart + infra repos: trunk = `main`, fleet-wide (`dev` being retired — no longer a commit
  target).
- Deploy repos: `dev` / `stage` / `main` are **environment** branches (the documented single-trunk
  exception) — `main` = prod.
- Routine work commits and pushes directly to `main`; use a feature branch/MR only when Chris
  explicitly requests a review gate.
- Do NOT create `develop`, `staging`, or `master` branches — and do not create a chart `dev` branch
  (`main` is the sole chart trunk). Feature branches off `main` for the MR flow are expected.

For Inline + Specialist paths, review the exact commit, rebase onto current `main`, and push it
directly to `main` after authorization. This direct-main lock-in supersedes both the older
direct-commit-on-`dev` preference and the interim routine-MR policy.

For Delegated path, use `{ID}-explorer-{a|b}` worktree branches — LOCAL-ONLY, never pushed.
Synthesize the chosen changes into ONE commit rebased onto current `main`, then `git worktree remove`
the explorer worktrees and push the reviewed commit directly to `main` after authorization.
Explorer branches are never publication inputs.

## Recording delivery intent and check evidence

Delivery-state v2 has no free-text external-write ledger. Preserve every local
validation command, exit code, timestamp, and output evidence for
`implementation-evidence.json`. Publication goes into `release-manifest.json`;
target mutations and their human authorizations go into the frozen
`operations-plan.json` plus append-only `operations-evidence.json`. These are
validated independently after rectification; an implementation checkpoint does
not claim publication or live operation.

## Checkpoint

```bash
# When implementation starts
.claude/scripts/milestone-pipeline-checkpoint.py {ID} implement-running

# Capture base sha BEFORE making any commits
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set "implementation_base=\"$(git rev-parse HEAD)\""

# After all commits + validation green (range + commit list DERIVED from the
# recorded base — never re-guessed from HEAD~1; round-4 F4)
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set "implementation_commit_range=\"<base>..$(git rev-parse HEAD)\""
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set "implementation_commits=$(git rev-list --reverse "<base>..HEAD" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')"
.claude/scripts/milestone-pipeline-checkpoint.py {ID} --set "implementation_branch=\"$(git branch --show-current)\""
.claude/scripts/milestone-pipeline-checkpoint.py {ID} implement-complete
```

Phase 3 critic reads from `state.implementation_commit_range` to compute the diff.
