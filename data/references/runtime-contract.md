---
type: reference
status: active
tags:
  - type/reference
  - status/active
  - domain/tooling
---
# Runtime Contract — where library code runs, and what it may assume

The execution contract for every skill, slash command, and script in this library. Read this
before writing or debugging any of them. Audience: any engineer/agent on any machine — macOS
laptop, Linux devcontainer, or an autonomous Sonnet-class session.

**When NOT to use this file:** repo/branch/commit rules → `git-topology.md` · fresh-machine
setup → `onboarding.md` · pipeline architecture (Gen-1 vs Gen-2, Workflow tool API) →
`pipeline-pattern-v2.md` · model-class semantics → `model-policy.md`.

## 1. Workspace-root resolution — the canonical pattern

Scripts and skills must NEVER hardcode a user home path and NEVER assume cwd. Resolve the
workspace like this (exact snippet — copy it):

```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
if [ ! -d "$WS" ]; then
  echo "Set PERSONAL_WORKSPACE_ROOT to your the workspace" >&2
  exit 1
fi
```

Derived anchors (the ONLY sanctioned ways to address library files):

```bash
PLATFORM="$WS/GitLab/workspace/platform"          # note the spaces — always quote
MCP_REPO="$PLATFORM/tools/claude-mcp-server"              # the library's git repo
# Library-internal references:
#   "$WS/.claude/scripts/<name>"     (symlink)  ≡  "$MCP_REPO/data/scripts/<name>"
#   "$WS/.claude/references/<name>"  (symlink)  ≡  "$MCP_REPO/data/references/<name>"
```

If your machine keeps the workspace elsewhere, export `PERSONAL_WORKSPACE_ROOT` (e.g. in your
shell profile) — every conforming script picks it up.

## 2. CWD contract per command family

Command bodies invoke scripts by **relative** path, so the cwd they assume matters. Verified
against `data/commands/*.md` (2026-07-02):

| Command family | Script paths used | Required cwd |
|---|---|---|
| Gen-1 pipelines: `/milestone-pipeline`, `/argoops`, `/spike`, `/roadmap` (init step) | `"$WS/.claude/scripts/..."` — **$WS-anchored, cwd-independent** | Run **inside the TARGET repo** (pipeline-pattern-v2.md §6); the `.claude/` symlink farm must resolve |
| `/roadmap` internal lint/scoring (`roadmap-validate.py`, `roadmap-score-*.py`), `/pipeline-builder`, `/skill-to-command` | `data/scripts/...` (relative) | **The claude-mcp-server checkout** (`$MCP_REPO`) |
| Gen-2 discovery pipelines (`/capability-scout`, `/cicd-uplift`, `/frontend-uplift`, `/interop-discovery`, `/mesh-as-code`, `/zerotrust-scout`) | `data/scripts/...` for init/status + `Workflow({scriptPath: "data/scripts/<x>-workflow.mjs"})` | **The claude-mcp-server checkout** (`$MCP_REPO`) |
| Outcome-log emits (several commands) | `.claude/scripts/pipeline-outcome-log.py` | Workspace root — note some commands MIX both forms |

Because some command files mix both forms, the robust move when a relative invocation fails is
to re-anchor absolutely: `"$WS/.claude/scripts/<x>"` or `"$MCP_REPO/data/scripts/<x>"` — they
are the same files (symlink).

Also remember (from `git-topology.md`): `$WS`, `$WS/GitLab`, and `$PLATFORM` are NOT git
repos — any script that runs `git rev-parse --show-toplevel` from those cwds fails. Commands
that derive a repo root must either be run from inside the target clone or handle the failure.

## 3. python3, not bash

`.py` scripts are ALWAYS invoked as `python3 <path>` — never `bash <path>`. Bash "runs" a
Python file by parsing it as shell: it either dies with parse errors (rc=2) or silently does
nothing useful. ~25 legacy `bash <x>.py` sites in `data/commands/*.md` and pipeline
references were swept in the 2026-07 overhaul (grep finds 0 remaining as of 2026-07-02); if
you ever encounter one, run it as `python3 <x>.py` and fix the doc. `.sh` scripts run with `bash <path>` (or directly if
executable). `.mjs` workflow scripts are NOT run by hand — they are passed to the Workflow
tool (§5).

## 4. Model policy — Sonnet floor

**Nothing in this library may hard-require Opus.** Sonnet-only model entitlement is the
supported floor. Per-agent model+effort is governed by `data/model-policy.json` (semantic
`model-class:` → resolution) and stamped by the apply script — never hand-edit `model:` in
agent frontmatter (`model-policy.md` has the full model).

The `deep-reasoning-*` classes currently resolve to `fable` (repointed 2026-07-09; each
class's fallback list holds `opus` then `sonnet`). If your plan/entitlement has no Fable
(or no Opus, once fallen back), dispatch of any deep-reasoning agent hard-fails — the
**sanctioned repoint** is:

```bash
cd "$MCP_REPO"
# 1. Edit data/model-policy.json: in the deep-reasoning-* classes, set "model": "sonnet"
#    (each class's "fallbacks" array documents the intended repoint order).
# 2. Restamp the fleet and verify:
python3 data/scripts/model-policy-apply.py
python3 data/scripts/model-policy-apply.py --check   # exit 0 = clean
```

For a personal, machine-local downshift instead of a shared repoint, use
`WORKSPACE_ROOT/.claude/model-policy.local.json` + `python3 data/scripts/model-policy-apply.py --local`
(does not touch shared files).

## 5. Workflow tool availability (Gen-2 pipelines)

The six Gen-2 discovery pipelines (§2 table) orchestrate through the **`Workflow`** background
tool (`Workflow({scriptPath: "data/scripts/<x>-workflow.mjs", ...})`). That tool is
harness-dependent and is NOT present in every Claude Code session. **Before dispatching a
Gen-2 pipeline, confirm `Workflow` is in the session's tool inventory.** If it is absent, do
not try to run the `.mjs` by hand — see `pipeline-pattern-v2.md` for the Gen-1 vs Gen-2 map
and the sanctioned fallback path.

## 6. First-run agent registration caveat

Subagent definitions in `data/agents/` (≡ `.claude/agents/`) are discovered at **session
start**. A freshly authored agent file is generally NOT dispatchable in the same session that
created it — the Agent tool reports the name unknown. Fix: start a new session (or restart),
then dispatch. Harness-version-dependent behavior; if a just-written agent won't dispatch,
assume this before debugging the agent body. (Same class of rule as the MCP server: `data/`
content is loaded once at startup — restart the session after content edits you need the MCP
tools to see.)

## 7. macOS vs Linux devcontainer

| Concern | macOS host | Linux devcontainer / sandbox |
|---|---|---|
| GPG for signed commits | `git -c gpg.program=/opt/homebrew/bin/gpg commit ...` (Homebrew path) | gpg is on PATH — plain `git commit -S ...`; do NOT hardcode the Homebrew path in scripts |
| AWS SSO login | Browser flow works directly | No browser in-container — the `/aws-refresh` skill handles it (host AWS-bridge mode first, `aws sso login --use-device-code` fallback). Run `/aws-refresh`; never ask the user to fix credentials by hand |
| Workspace path | Typically `$HOME/Work/workspace` (the §1 default) | Set `PERSONAL_WORKSPACE_ROOT` explicitly — the default likely doesn't exist |
| `.claude/` farm | Symlinks into `data/` (planted by `setup-local.sh --symlink`) | Depends on bootstrap — see `onboarding.md` |

## 8. Non-negotiables inherited from the workspace tier

These are stated once in the workspace root `CLAUDE.md` and bind every script/skill/agent:
External System Write Policy (every external write gated on the confirmation of the engineer
driving the session), the deploy-repo edit rules, and the terraform-apply hard gate for new
repos. Nothing in this contract routes around them.

An authorization covers effects, not just the immediate executable. If a source
push is known to conditionally trigger CI rendering, a protected deploy-branch
write, and Argo auto-sync, every material edge and exact target must be present
in the previewed scope before the push; a generic controller label is never
enough. A later observer may record that preauthorized controller effect but
must not execute or replay it. Also classify bounded remote execution by its
effect: `kubectl exec ... curl` is an active verification action even when the
HTTP request is read-only, so its executable, context, argv, and timeout belong
to the authorized action surface.
