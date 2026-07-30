# template-settings.json — the tracked Claude Code enforcement baseline

`template-settings.json` (sibling file) is the git-tracked baseline for the workspace
project settings at `<workspace>/.claude/settings.json`. JSON cannot carry comments, so
every entry is documented here. Nothing merges it automatically yet: `agent-kit init`
plants the knowledge tree but not `settings.json`, so copy the entries you want into
`<workspace>/.claude/settings.json` by hand. A merging planter that adds only absent
keys is future work; until it exists, this file is a reference, not an installer input.

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

Edit `template-settings.json` + this doc together. Because the merge is manual today,
a *changed* entry will not propagate on its own: engineers must remove the old entry
from their live `settings.json` once, or delete the file and re-copy
bootstrap). Commit both files and push to `origin/main` per the sharing rule in the repo
CLAUDE.md.


## Which hooks are on by default, and what each blocks

Registered by `template-settings.json`. Every path is rooted at
`${CLAUDE_PROJECT_DIR}` — there is no machine-specific fallback, and
`template-settings-check.py --check` fails if a registered path does not resolve
to something the installer plants.

| Event | Matcher | Hook | Blocks |
|---|---|---|---|
| PreToolUse | `Bash` | `validate-commit-subject.sh` | Commit subjects that violate the repo's subject contract |
| PreToolUse | `Bash` | `block-plaintext-secret-write.sh` | Shell commands that would write a credential in plaintext |
| PreToolUse | `Edit\|Write` | `block-deploy-repo-edits.sh` | Direct edits to a deploy repo, which must be generated |
| PreToolUse | `Edit\|Write` | `block-plaintext-secret-write.sh` | Writing a credential into a tracked file |
| PostToolUse | `Edit\|Write` | `memory-sync-reminder.sh` | Nothing — it reminds, it does not block |

**Not registered by default:** `block-kubectl-mutations.sh`. It lives in
`template-settings.k8s.json`, an opt-in overlay, together with the read-only
`kubectl` grants it guards. Registering a Kubernetes guard in the default
template — for a kit that ships no Kubernetes anything — would grant permissions
nobody asked for and imply a capability the kit does not have.

**Fail-open, but never silent.** A hook whose target is missing exits 0, because a
broken hook must not wedge a session. It first writes one line to stderr naming
the path it looked for. A guard you believe is running and which is not is worse
than no guard, and the previous `[ -f "$h" ] || exit 0` said nothing at all.
