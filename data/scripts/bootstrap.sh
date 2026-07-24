#!/usr/bin/env bash
# bootstrap.sh — full workstation bootstrap for the Claude Code library.
#
# Turns a machine with git + a clone of THIS repo (tools/claude-mcp-server) into a
# fully wired engineer workstation: ~70 platform clones, the .claude symlink farm
# (workspace AND platform tiers), workspace-tier CLAUDE.md copies, the tracked
# settings/hooks enforcement baseline, .mcp.json + launch.json, git hooks, the
# obsidian clean filter, agent-memory consolidation, and a built MCP server —
# ending with a pass/fail verification checklist.
#
# IDEMPOTENT: re-running repairs/completes; it never overwrites an existing
# .mcp.json / launch.json and never clobbers keys you already have in
# .claude/settings.json. Works on macOS and Linux (bash 3.2+, stdlib tools).
#
# USAGE
#   bash data/scripts/bootstrap.sh                # interactive (prompts once for the workspace root)
#   bash data/scripts/bootstrap.sh --dry-run      # print every action, change nothing
#   bash data/scripts/bootstrap.sh --clone        # also clone all missing platform repos (repos-manifest.tsv)
#   bash data/scripts/bootstrap.sh --no-build     # skip npm ci / npm run build (e.g. offline)
#   PERSONAL_WORKSPACE_ROOT=~/Work/workspace bash data/scripts/bootstrap.sh   # non-interactive root
#
# The end-to-end day-1 sequence (prereqs, tokens, SSO, verification) is documented
# in data/references/onboarding.md — read that first on a fresh machine.
set -uo pipefail

DRY_RUN=0
DO_CLONE=0
DO_BUILD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --clone)    DO_CLONE=1; shift ;;
    --no-build) DO_BUILD=0; shift ;;
    -h|--help)  awk 'NR>1 && /^[^#]/{exit} NR>1{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; RESET=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s[ok]%s   %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '%s[err]%s  %s\n' "$RED" "$RESET" "$*" >&2; }
head_() { printf '\n%s%s==> %s%s\n' "$BOLD" "$CYAN" "$*" "$RESET"; }
# run <description> -- <cmd...> : executes, or prints in --dry-run.
run() {
  local desc="$1"; shift; [[ "$1" == "--" ]] && shift
  if [[ "$DRY_RUN" == 1 ]]; then
    say "  DRY-RUN: would $desc:  $*"
    return 0
  fi
  say "  $desc"
  "$@"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # data/scripts -> claude-mcp-server

# ============================================================================
# 1. Prerequisites (versions per package.json engines + hook/script needs)
# ============================================================================
head_ "Step 1/10 — prerequisite checks"
MISSING=0
need() { # need <cmd> <why> [hard|soft]
  local cmd="$1" why="$2" sev="${3:-hard}"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd — $why"
  elif [[ "$sev" == hard ]]; then
    err "MISSING: $cmd — $why"; MISSING=1
  else
    warn "missing (optional now, needed later): $cmd — $why"
  fi
}
need git   "clones + hooks"
need node  "MCP server runtime (>= 20 per package.json engines)"
need npm   "MCP server build"
need python3 "library scripts (ALWAYS 'python3 <script>.py', never 'bash')"
need jq    "Claude Code PreToolUse hooks (validate-commit-subject, blockers)"
need curl  "pre-push CI lint (GitLab API)"
need gpg   "signed commits (server-side push rule)" soft
need aws   "AWS SSO profiles / live cluster access" soft
need kubectl "cluster read access (skills/pipelines)" soft
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node --version | sed 's/^v//;s/\..*$//')"
  if [[ "$NODE_MAJOR" -lt 20 ]]; then
    err "Node.js $(node --version) is too old — package.json requires >= 20 (nvm install 20)"; MISSING=1
  fi
fi
[[ "$MISSING" == 1 && "$DRY_RUN" == 0 ]] && { err "Install the missing hard prerequisites, then re-run."; exit 1; }

# ============================================================================
# 2. Resolve PERSONAL_WORKSPACE_ROOT
# ============================================================================
head_ "Step 2/10 — workspace root"
WS="${PERSONAL_WORKSPACE_ROOT:-}"
if [[ -z "$WS" ]]; then
  # If this clone already sits inside a workspace-shaped tree, derive it:
  # <ws>/GitLab/workspace/platform/tools/claude-mcp-server
  guess="$(cd "$REPO_ROOT/../../../../.." 2>/dev/null && pwd || true)"
  if [[ -n "$guess" && -d "$guess/GitLab" ]]; then
    WS="$guess"
    ok "derived from this clone's location: $WS"
  elif [[ -t 0 && "$DRY_RUN" == 0 ]]; then
    printf 'Where should the workspace live? [default: %s/Work/workspace]: ' "$HOME"
    read -r answer
    WS="${answer:-$HOME/Work/workspace}"
  else
    WS="$HOME/Work/workspace"
    warn "PERSONAL_WORKSPACE_ROOT not set and no TTY — defaulting to $WS"
  fi
fi
say "  PERSONAL_WORKSPACE_ROOT = $WS"
PLATFORM_ROOT="$WS/GitLab/workspace/platform"
CANON_REPO="$PLATFORM_ROOT/tools/claude-mcp-server"
run "create workspace skeleton" -- mkdir -p "$PLATFORM_ROOT" "$WS/.claude"

# Persist the export line (sourceable snippet; the user adds ONE line to their profile).
ENV_SNIPPET="$HOME/.workspace-env.sh"
if [[ ! -f "$ENV_SNIPPET" ]] || ! grep -q "PERSONAL_WORKSPACE_ROOT=" "$ENV_SNIPPET" 2>/dev/null; then
  if [[ "$DRY_RUN" == 1 ]]; then
    say "  DRY-RUN: would write export line to $ENV_SNIPPET"
  else
    printf 'export PERSONAL_WORKSPACE_ROOT=%q\n' "$WS" > "$ENV_SNIPPET"
    ok "wrote $ENV_SNIPPET"
  fi
fi
say ""
say "  ${BOLD}Add this line to your shell profile (~/.zshrc or ~/.bashrc) if not present:${RESET}"
say "      source \$HOME/.workspace-env.sh"

# ============================================================================
# 3. Clone the platform repos (optional — needs SSH access to gitlab-internal)
# ============================================================================
head_ "Step 3/10 — platform clones (repos-manifest.tsv)"
if [[ "$DO_CLONE" == 1 ]]; then
  if [[ "$DRY_RUN" == 1 ]]; then
    bash "$SCRIPT_DIR/clone-workspace.sh" --dry-run --workspace "$WS" || true
  else
    bash "$SCRIPT_DIR/clone-workspace.sh" --workspace "$WS" || warn "some clones failed — see above; re-run after fixing SSH access"
  fi
else
  say "  skipped (pass --clone to clone all ~70 platform repos; needs SSH to gitlab.example.com)"
fi

# If this bootstrap is running from a clone OUTSIDE the canonical location and the
# canonical clone now exists, hand over to it so every later step uses canonical paths.
if [[ "$REPO_ROOT" != "$CANON_REPO" && -d "$CANON_REPO/data/scripts" && -z "${BOOTSTRAP_DELEGATED:-}" && "$DRY_RUN" == 0 ]]; then
  warn "this clone is not at the canonical path — continuing from $CANON_REPO"
  BOOTSTRAP_DELEGATED=1 PERSONAL_WORKSPACE_ROOT="$WS" exec bash "$CANON_REPO/data/scripts/bootstrap.sh" \
    $( [[ "$DO_CLONE" == 1 ]] && echo --clone ) $( [[ "$DO_BUILD" == 0 ]] && echo --no-build )
fi
ACTIVE_REPO="$REPO_ROOT"
[[ -d "$CANON_REPO/data/scripts" ]] && ACTIVE_REPO="$CANON_REPO"

# ============================================================================
# 4. Build + symlink farm + CLAUDE.md copies (setup-local.sh --symlink)
# ============================================================================
head_ "Step 4/10 — MCP server build + .claude symlink farm (workspace tier)"
if [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would run: bash '$ACTIVE_REPO/setup-local.sh' --symlink"
  say "           (npm ci + npm run build; symlinks .claude/{skills,agents,references,scripts,hooks,commands};"
  say "            plants workspace/GitLab/platform CLAUDE.md + AGENTS.md copies BODY-ONLY)"
elif [[ "$DO_BUILD" == 1 ]]; then
  bash "$ACTIVE_REPO/setup-local.sh" --symlink || { err "setup-local.sh failed"; exit 1; }
else
  warn "--no-build: skipping setup-local.sh build; planting symlinks directly"
  for d in skills agents references scripts hooks commands; do
    [[ -d "$ACTIVE_REPO/data/$d" ]] && ln -sfn "$ACTIVE_REPO/data/$d" "$WS/.claude/$d"
  done
fi

# ============================================================================
# 5. Platform-tier .claude farm + AGENTS.md pointer (platform-rooted sessions)
# ============================================================================
head_ "Step 5/10 — .claude farm at platform/ (so platform-rooted sessions get the same library)"
for d in agents commands skills references scripts hooks; do
  if [[ -d "$ACTIVE_REPO/data/$d" ]]; then
    run "symlink platform/.claude/$d" -- mkdir -p "$PLATFORM_ROOT/.claude"
    run "  -> data/$d" -- ln -sfn "$ACTIVE_REPO/data/$d" "$PLATFORM_ROOT/.claude/$d"
  fi
done
PTR="$PLATFORM_ROOT/AGENTS.md"
if [[ ! -f "$PTR" ]]; then
  if [[ "$DRY_RUN" == 1 ]]; then
    say "  DRY-RUN: would write pointer file $PTR"
  else
    cat > "$PTR" <<'EOF'
# AGENTS.md — pointer

The agent registry for this workspace lives at the workspace root: `../../../AGENTS.md`
(canonical master: `tools/claude-mcp-server/data/claude-md/AGENTS.md` — one of the four managed copy pairs; see `data/scripts/claude-md-copy-lint.sh`). This file is deliberately a pointer, not a fifth copy: unmanaged copies drift (the previous content of this file described the pre-migration old-account platform for over a year).
EOF
    ok "wrote pointer $PTR"
  fi
elif ! grep -q 'AGENTS.md — pointer' "$PTR" 2>/dev/null; then
  warn "$PTR exists with non-pointer content — leaving it; consider replacing with the 3-line pointer"
fi

# ============================================================================
# 6. Settings baseline merge (template-settings.json -> .claude/settings.json)
# ============================================================================
head_ "Step 6/10 — enforcement baseline (template-settings.json; see template-settings.md)"
SETTINGS="$WS/.claude/settings.json"
if [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would merge $SCRIPT_DIR/template-settings.json into $SETTINGS (add-only, never clobbers)"
else
  python3 - "$SCRIPT_DIR/template-settings.json" "$SETTINGS" <<'PY'
import json, os, sys

tmpl_path, live_path = sys.argv[1], sys.argv[2]
with open(tmpl_path) as f:
    tmpl = json.load(f)
live = {}
if os.path.exists(live_path):
    with open(live_path) as f:
        live = json.load(f)
changed = []

env = live.setdefault("env", {})
for k, v in tmpl.get("env", {}).items():
    if k not in env:
        env[k] = v
        changed.append(f"env.{k}")

allow = live.setdefault("permissions", {}).setdefault("allow", [])
for p in tmpl.get("permissions", {}).get("allow", []):
    if p not in allow:
        allow.append(p)
        changed.append(f"permissions.allow: {p}")

def script_name(cmd):
    # identify a hook by the .sh basename it references (robust to wrapper changes)
    import re
    m = re.findall(r"([A-Za-z0-9_-]+\.sh)", cmd or "")
    return m[-1] if m else (cmd or "")

hooks = live.setdefault("hooks", {})
for event, tentries in tmpl.get("hooks", {}).items():
    lentries = hooks.setdefault(event, [])
    for te in tentries:
        same = [le for le in lentries if le.get("matcher") == te.get("matcher")]
        if not same:
            lentries.append(te)
            changed.append(f"hooks.{event} matcher={te.get('matcher')}")
            continue
        le = same[0]
        lhooks = le.setdefault("hooks", [])
        existing = {script_name(h.get("command")) for h in lhooks}
        for h in te.get("hooks", []):
            if script_name(h.get("command")) not in existing:
                lhooks.append(h)
                changed.append(f"hooks.{event}/{te.get('matcher')}: {script_name(h.get('command'))}")

if changed:
    with open(live_path, "w") as f:
        json.dump(live, f, indent=2)
        f.write("\n")
    print("  merged baseline additions:")
    for c in changed:
        print(f"    + {c}")
else:
    print("  settings.json already carries the full baseline — no change")
PY
fi

# ============================================================================
# 7. Workspace config files from templates (create-if-absent, NEVER overwrite)
# ============================================================================
head_ "Step 7/10 — .mcp.json + launch.json (create-if-absent)"
MCP_JSON="$WS/.mcp.json"
if [[ -f "$MCP_JSON" ]]; then
  ok ".mcp.json exists — not touched"
elif [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would create $MCP_JSON from template-mcp.json (workspace path substituted;"
  say "           atlassian creds live ONLY in ~/.config/workspace/secrets.env — the wrapper sources it at spawn)"
else
  # Substitute the workspace path at install time (deterministic even if the shell
  # profile isn't wired yet); leave secret ${VAR}s for Claude Code's runtime env expansion.
  WS_ESCAPED="$WS" python3 - "$SCRIPT_DIR/template-mcp.json" "$MCP_JSON" <<'PY'
import os, sys
text = open(sys.argv[1]).read()
text = text.replace("${PERSONAL_WORKSPACE_ROOT}", os.environ["WS_ESCAPED"])
open(sys.argv[2], "w").write(text)
PY
  ok "created $MCP_JSON (define CONFLUENCE_USERNAME + ATLASSIAN_API_TOKEN in ~/.config/workspace/secrets.env, chmod 600 — one home per secret)"
fi
LAUNCH="$WS/.claude/launch.json"
if [[ -f "$LAUNCH" ]]; then
  ok "launch.json exists — not touched"
else
  run "create $LAUNCH from template-launch.json" -- cp "$SCRIPT_DIR/template-launch.json" "$LAUNCH"
fi
# .codex/config.toml — indirect-by-default (S1.1): the template's bash -c wrapper
# sources ~/.config/workspace/secrets.env at launch; no credential literal in the file.
CODEX_TOML="$WS/.codex/config.toml"
if [[ -f "$CODEX_TOML" ]]; then
  ok ".codex/config.toml exists — not touched"
elif [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would create $CODEX_TOML from template-codex-config.toml (workspace path substituted;"
  say "           secrets stay in ~/.config/workspace/secrets.env — never in the file)"
else
  mkdir -p "$WS/.codex"
  WS_ESCAPED="$WS" python3 - "$SCRIPT_DIR/template-codex-config.toml" "$CODEX_TOML" <<'PY'
import os, sys
text = open(sys.argv[1]).read()
text = text.replace("${PERSONAL_WORKSPACE_ROOT}", os.environ["WS_ESCAPED"])
open(sys.argv[2], "w").write(text)
PY
  ok "created $CODEX_TOML (define CONFLUENCE_USERNAME + CONFLUENCE_API_TOKEN in ~/.config/workspace/secrets.env, chmod 600)"
fi

# ============================================================================
# 8. Git hooks + clean filter + gitignore hygiene (per-clone, not committed)
# ============================================================================
head_ "Step 8/10 — git hooks (pre-push CI lint), obsidian filter, gitignore hygiene"
if [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would run: bash '$SCRIPT_DIR/install-cilint-hook.sh' --all   (pre-push shim into every platform clone)"
  say "  DRY-RUN: would run: (cd '$ACTIVE_REPO' && bash data/scripts/obsidian-setup.sh)   (clean-filter, this repo only)"
  say "  DRY-RUN: would run: bash '$SCRIPT_DIR/ensure-claude-gitignore.sh' '$ACTIVE_REPO'"
else
  PLATFORM_ROOT="$PLATFORM_ROOT" bash "$SCRIPT_DIR/install-cilint-hook.sh" --all || warn "install-cilint-hook --all reported issues (see above)"
  ( cd "$ACTIVE_REPO" && bash data/scripts/obsidian-setup.sh ) || warn "obsidian-setup failed (non-fatal: *.md commits keep volatile frontmatter)"
  bash "$SCRIPT_DIR/ensure-claude-gitignore.sh" "$ACTIVE_REPO"
  say "  note: other repos self-protect — every pipeline *-init.sh calls ensure-claude-gitignore.sh on first run"
fi

# ============================================================================
# 9. Agent-memory consolidation (idempotent symlink farm for lessons)
# ============================================================================
head_ "Step 9/10 — agent-memory consolidation"
if [[ "$DRY_RUN" == 1 ]]; then
  say "  DRY-RUN: would run: WORKSPACE_ROOT='$WS' bash '$SCRIPT_DIR/agent-memory-consolidate.sh' --apply"
else
  WORKSPACE_ROOT="$WS" bash "$SCRIPT_DIR/agent-memory-consolidate.sh" --apply --quiet || warn "agent-memory consolidation reported issues (non-fatal; agents degrade to cold memory)"
  ok "unified agent-memory root: $WS/.claude/agent-memory (starts EMPTY on a fresh machine — lessons are per-machine)"
fi

# ============================================================================
# 10. VERIFY — pass/fail checklist
# ============================================================================
head_ "Step 10/10 — verification"
FAIL=0
check() { # check <label> <cmd...>
  local label="$1"; shift
  if [[ "$DRY_RUN" == 1 ]]; then say "  DRY-RUN: would check: $label"; return 0; fi
  if "$@" >/dev/null 2>&1; then
    ok "$label"
  else
    err "FAIL: $label"
    FAIL=1
  fi
}
for d in agents commands hooks references scripts skills; do
  check "workspace symlink .claude/$d resolves" test -d "$WS/.claude/$d/"
done
for d in agents commands hooks references scripts skills; do
  check "platform symlink platform/.claude/$d resolves" test -d "$PLATFORM_ROOT/.claude/$d/"
done
check "CLAUDE.md copies in sync (claude-md-copy-lint)" bash "$ACTIVE_REPO/data/scripts/claude-md-copy-lint.sh"
check "model policy clean (model-policy-apply --check)" python3 "$ACTIVE_REPO/data/scripts/model-policy-apply.py" --check
check "MCP server built (dist/index.js)" test -f "$ACTIVE_REPO/dist/index.js"
check ".mcp.json present" test -f "$WS/.mcp.json"
check "settings baseline present (.claude/settings.json)" test -f "$WS/.claude/settings.json"
check "pre-push hook installed in claude-mcp-server" grep -q pre-push-cilint "$ACTIVE_REPO/.git/hooks/pre-push"
check "kubectl-mutation blocker present" test -f "$WS/.claude/hooks/block-kubectl-mutations.sh"
check "deploy-repo edit blocker present" test -f "$WS/.claude/hooks/block-deploy-repo-edits.sh"

say ""
if [[ "$DRY_RUN" == 1 ]]; then
  say "${BOLD}DRY RUN complete — nothing was changed.${RESET} Re-run without --dry-run to apply."
elif [[ "$FAIL" == 1 ]]; then
  err "Bootstrap finished with FAILED checks — fix the items above and re-run (idempotent)."
  exit 1
else
  say "${GREEN}${BOLD}Bootstrap complete.${RESET} Next: restart Claude Code from $WS so it loads .mcp.json,"
  say "then follow the first-session orientation in data/references/onboarding.md"
  say "(read platform-orientation.md -> glossary.md -> git-topology.md; try /argocd-status)."
fi
