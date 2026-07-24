# template-settings.json — the tracked Claude Code enforcement baseline

`template-settings.json` (sibling file) is the git-tracked baseline for the workspace
project settings at `<workspace>/.claude/settings.json`. JSON cannot carry comments, so
every entry is documented here. `data/scripts/bootstrap.sh` merges it into the live
settings file **without clobbering keys you already have** (env keys are added only if
absent, permission entries and hook entries are appended only if not already present).

**Why this file exists:** before 2026-07, the entire mechanical safety layer (kubectl
blocker, deploy-repo edit blocker, commit-subject gate) lived only in the gitignored
`settings.local.json` on the platform owner's machine — and two of the three inline
blockers were silently inert (a `grep "--dry-run"` pattern parsed as a grep OPTION, and a
`gitops-config/` path pattern predating the account migration). The blockers are now
tracked FILES in `data/hooks/` with tests-by-hand documented in each header, and this
template wires them for every engineer.

## What each entry does

### `env`

| Key | Why |
|---|---|
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` | Enables the agent-teams harness feature used by the multi-agent pipelines (matches the reference machine's live setting). Harmless if the CLI version ignores it. |

### `permissions.allow`

A deliberately small read-only baseline so a fresh machine is not prompted for every
`kubectl get`. The **enforcement inversion** is intentional: `kubectl` is broadly
allowed at the permission layer, and mutations are gated mechanically by the
`block-kubectl-mutations.sh` PreToolUse hook (hooks run regardless of permission
allowlists). Grow your own machine-local allowlist in `settings.local.json` (the
`/fewer-permission-prompts` skill curates one) — do not grow this shared baseline
without owner review.

### `hooks.PreToolUse`

Each hook command is a thin resolver wrapper:

```bash
bash -c 'h="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/hooks/<name>.sh"; \
  [ -f "$h" ] || h="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}/.claude/hooks/<name>.sh"; \
  [ -f "$h" ] && exec bash "$h"; exit 0'
```

— it prefers the project-local `.claude/hooks/` (a symlink into `data/hooks/`), falls
back to the workspace farm via `PERSONAL_WORKSPACE_ROOT` (so the guard also fires in
sessions rooted in an individual repo that has no `.claude/` of its own), and **fails
open** (exit 0) if the hook file is absent — a missing hook must not block unrelated
work. All three hook scripts read the PreToolUse JSON payload on **stdin** and emit a
structured `permissionDecision` (the mechanics proven by
`validate-commit-subject.sh`; do NOT switch back to `$TOOL_INPUT` env-var matching).

| Matcher | Hook file (in `data/hooks/`) | Blocks / gates |
|---|---|---|
| `Bash` | `block-kubectl-mutations.sh` | **Gates (`permissionDecision: ask`)** `kubectl apply/create/delete/patch/edit/scale/annotate/label/cordon/drain/taint/replace/rollout {restart,undo,pause,resume}` without `--dry-run` — the harness prompt is the per-command confirmation gate, so user-approved gated remediations (kargo reverify, /argoops, /argocd-unstick, /eks-node-replace) remain executable after explicit approval. Read-only kubectl (incl. `rollout status`) passes without a prompt. Quoted text (`echo "kubectl delete"`) does not trigger. |
| `Bash` | `validate-commit-subject.sh` | `git commit -m` without `-S` (GPG) or with a non-conventional-commit subject. Editor-mode commits (no `-m`) pass through to git's own hooks. |
| `Edit\|Write` | `block-deploy-repo-edits.sh` | Any Edit/Write whose file path is inside `deploy/argocd-config-*/` (current layout) or matches `gitops-config/apps/**manifest.yaml` (legacy layout). **Carve-out:** `deploy/argocd-config-*/applicationsets/**` is hand-maintained (owner-approved exception, see platform/CLAUDE.md "GitOps Guardrail") and is not blocked. |

Known limitation (all path-matching hooks): they match the path **string** the tool was
given. A session whose cwd is *inside* the deploy repo passing bare relative paths is
not caught — don't root sessions in deploy repos.

### `hooks.PostToolUse`

| Hook | Behavior |
|---|---|
| `memory-sync-reminder.sh` (in `data/scripts/`) | After an Edit/Write that touches a durable-memory surface (`.claude/agent-memory/*/lessons.md`, `platform/source/*/CLAUDE.md`), emits a one-line nudge to consider `/memory-sync`. Never blocks. |
| `scripts/project-linker-hook.sh` | **Optional, machine-local** Obsidian/vault tooling that lives at the workspace root *outside* git. The wrapper is existence-guarded: on machines without it (every machine but the original author's), the hook silently no-ops. |

## What this template deliberately does NOT contain

- **Machine-local permission accumulation** — that belongs in `settings.local.json`
  (excluded by the `ensure-claude-gitignore.sh` block in every repo — `.git/info/exclude`
  since 2026-07-09).
- **`enabledMcpjsonServers` / `autoMode`** — per-machine choices.
- **SessionStart/OTel telemetry hooks** — personal tooling on the reference machine,
  not part of the platform baseline.

## Updating the baseline

Edit `template-settings.json` + this doc together, then re-run
`bash data/scripts/bootstrap.sh` (idempotent — the merge adds only what is missing; it
never rewrites existing entries, so if you *change* a hook command here, engineers must
remove the old entry from their live `settings.json` once, or delete the file and re-run
bootstrap). Commit both files and push to `origin/main` per the sharing rule in the repo
CLAUDE.md.
