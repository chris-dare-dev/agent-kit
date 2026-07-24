#!/usr/bin/env bash
# setup-local.sh — Bootstrap the platform MCP server for local Claude Code use.
#
# What this script does:
#   1. Detects PLATFORM_ROOT (parent with charts/ and tools/)
#   2. Detects WORKSPACE_ROOT (parent with GitLab/)
#   3. Checks Node.js >= 20 is installed
#   4. Runs npm install && npm run build
#   5. Plants the .claude/ farm (SYMLINKS by default — copies permanently shadow
#      canonical data/ as it evolves; use --copy only on filesystems without
#      symlink support) and the workspace-tier CLAUDE.md/AGENTS.md copies
#      (BODY ONLY — any leading Obsidian YAML frontmatter in data/claude-md/ is
#      stripped; claude-md-copy-lint.sh compares bodies the same way)
#   6. Prints the claude mcp add command and ~/.claude/settings.json snippet
#
# NOTE: for a FULL fresh-machine setup (clones, hooks, settings baseline,
# .mcp.json, verification) run data/scripts/bootstrap.sh instead — it calls
# this script for the build + farm steps. See data/references/onboarding.md.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE="symlink"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --symlink) MODE="symlink"; shift ;;
    --copy)    MODE="copy"; shift ;;
    --build-only) MODE="build-only"; shift ;;
    -h|--help) echo "Usage: setup-local.sh [--symlink (default)|--copy|--build-only]"; exit 0 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { printf "${GREEN}[ok]${RESET}  %s\n" "$*"; }
warn()    { printf "${YELLOW}[warn]${RESET} %s\n" "$*"; }
error()   { printf "${RED}[err]${RESET}  %s\n" "$*" >&2; }
fatal()   { error "$*"; exit 1; }
heading() { printf "\n${BOLD}${CYAN}==> %s${RESET}\n" "$*"; }
step()    { printf "${CYAN}---${RESET} %s\n" "$*"; }

# ---------------------------------------------------------------------------
# 1. Resolve the directory that contains this script
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 2. Detect PLATFORM_ROOT — walk up until we find a directory containing
#    both charts/ and tools/. (NOT sandbox/ — sandbox is an optional extra
#    clone that fresh machines built by clone-workspace.sh do not have.)
# ---------------------------------------------------------------------------
heading "Detecting PLATFORM_ROOT"

detect_platform_root() {
  local dir="$1"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/charts" && -d "$dir/tools" ]]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

if PLATFORM_ROOT="$(detect_platform_root "$SCRIPT_DIR")"; then
  info "PLATFORM_ROOT = $PLATFORM_ROOT"
else
  fatal "Could not find a parent directory containing both charts/ and tools/. Is the script inside the platform directory tree?"
fi

# ---------------------------------------------------------------------------
# 3. Detect WORKSPACE_ROOT — walk up until we find a directory containing
#    a GitLab/ subdirectory.
# ---------------------------------------------------------------------------
heading "Detecting WORKSPACE_ROOT"

detect_workspace_root() {
  local dir="$1"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/GitLab" ]]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

if WORKSPACE_ROOT="$(detect_workspace_root "$SCRIPT_DIR")"; then
  info "WORKSPACE_ROOT = $WORKSPACE_ROOT"
else
  fatal "Could not find a parent directory containing GitLab/. Is the script inside the workspace?"
fi

# ---------------------------------------------------------------------------
# 4. Check Node.js >= 20
# ---------------------------------------------------------------------------
heading "Checking Node.js version"

if ! command -v node &>/dev/null; then
  fatal "node is not installed. Install Node.js >= 20 (e.g. via nvm: nvm install 20)"
fi

NODE_VERSION="$(node --version)"          # e.g. v22.3.0
NODE_MAJOR="${NODE_VERSION#v}"            # strip leading 'v'
NODE_MAJOR="${NODE_MAJOR%%.*}"            # keep major only

if [[ "$NODE_MAJOR" -lt 20 ]]; then
  fatal "Node.js $NODE_VERSION is too old. Require >= 20. Current: $NODE_VERSION"
fi

info "Node.js $NODE_VERSION (>= 20 required)"

# ---------------------------------------------------------------------------
# 5. npm install + npm run build
# ---------------------------------------------------------------------------
heading "Building MCP server"

cd "$SCRIPT_DIR"
if [[ -f package-lock.json ]]; then
  step "Running npm ci in $SCRIPT_DIR (lockfile present — reproducible install)"
  npm ci
else
  step "Running npm install in $SCRIPT_DIR"
  npm install
fi

step "Running npm run build"
npm run build

info "Build complete — compiled output in $SCRIPT_DIR/dist/"

# ---------------------------------------------------------------------------
# 6. Plant workspace files (skip if --build-only)
# ---------------------------------------------------------------------------
if [ "${MODE}" != "build-only" ]; then
  echo ""
  echo "--- Planting workspace files (mode: ${MODE}) ---"

  DATA_DIR="${SCRIPT_DIR}/data"

  if [ ! -d "${DATA_DIR}/skills" ]; then
    echo -e "${RED}ERROR: data/ directory not populated. Run from the MCP server repo.${RESET}"
    exit 1
  fi

  # Create .claude/ if needed
  mkdir -p "${WORKSPACE_ROOT}/.claude"

  if [ "${MODE}" = "symlink" ]; then
    # Symlink each subdirectory
    for subdir in skills agents references scripts hooks; do
      if [ -d "${DATA_DIR}/${subdir}" ]; then
        ln -sfn "${DATA_DIR}/${subdir}" "${WORKSPACE_ROOT}/.claude/${subdir}"
        echo -e "  ${GREEN}LINKED${RESET} .claude/${subdir} -> data/${subdir}"
      fi
    done

    # commands — Claude Code CLI natively discovers .claude/commands/*.md as slash commands
    if [ -d "${DATA_DIR}/commands" ]; then
      # If .claude/commands/ already exists as a real directory (e.g. from
      # an earlier sandbox-copy run or manual setup), ln -sfn silently fails
      # on macOS rather than replacing the directory. Remove it first to
      # match the copy-mode branch's behavior.
      if [ -e "${WORKSPACE_ROOT}/.claude/commands" ] && [ ! -L "${WORKSPACE_ROOT}/.claude/commands" ]; then
        rm -rf "${WORKSPACE_ROOT}/.claude/commands"
      fi
      ln -sfn "${DATA_DIR}/commands" "${WORKSPACE_ROOT}/.claude/commands"
      echo -e "  ${GREEN}LINKED${RESET} .claude/commands -> data/commands"
    fi
  else
    # Copy each subdirectory
    for subdir in skills agents references scripts hooks; do
      if [ -d "${DATA_DIR}/${subdir}" ]; then
        rm -rf "${WORKSPACE_ROOT}/.claude/${subdir}"
        cp -R "${DATA_DIR}/${subdir}" "${WORKSPACE_ROOT}/.claude/${subdir}"
        echo -e "  ${GREEN}COPIED${RESET} .claude/${subdir}"
      fi
    done

    # commands — Claude Code CLI natively discovers .claude/commands/*.md as slash commands
    if [ -d "${DATA_DIR}/commands" ]; then
      rm -rf "${WORKSPACE_ROOT}/.claude/commands"
      cp -R "${DATA_DIR}/commands" "${WORKSPACE_ROOT}/.claude/commands"
      echo -e "  ${GREEN}COPIED${RESET} .claude/commands"
    fi
  fi

  # Plant CLAUDE.md files at expected locations — BODY ONLY.
  # The data/claude-md/ masters may carry a leading Obsidian-vault YAML
  # frontmatter block (stamped by vault tooling, kept in git); the ACTIVE
  # copies Claude Code auto-loads must not. claude-md-copy-lint.sh compares
  # bodies the same way (frontmatter-tolerant), so a body-only plant is in
  # sync by definition.
  plant_claude_md() {
    local src="$1" dst="$2" label="$3"
    [ -f "$src" ] || return 0
    python3 - "$src" "$dst" <<'PY' && echo -e "  ${GREEN}PLANTED${RESET} ${label} (body-only)" || true
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
if text.startswith("---\n"):
    end = text.find("\n---\n", 4)
    if end != -1:
        text = text[end + 5 :].lstrip("\n")
open(dst, "w", encoding="utf-8").write(text)
PY
  }

  if [ -n "${WORKSPACE_ROOT}" ]; then
    plant_claude_md "${DATA_DIR}/claude-md/workspace-root.md" "${WORKSPACE_ROOT}/CLAUDE.md" "CLAUDE.md at workspace root"

    if [ -d "${WORKSPACE_ROOT}/GitLab" ]; then
      plant_claude_md "${DATA_DIR}/claude-md/gitlab-domain.md" "${WORKSPACE_ROOT}/GitLab/CLAUDE.md" "CLAUDE.md at GitLab/"
    fi

    plant_claude_md "${DATA_DIR}/claude-md/AGENTS.md" "${WORKSPACE_ROOT}/AGENTS.md" "AGENTS.md at workspace root"
  fi

  if [ -n "${PLATFORM_ROOT}" ]; then
    plant_claude_md "${DATA_DIR}/claude-md/platform-monorepo.md" "${PLATFORM_ROOT}/CLAUDE.md" "CLAUDE.md at platform root"
  fi

  # Summary
  SKILL_COUNT=$(ls "${DATA_DIR}/skills" 2>/dev/null | wc -l | tr -d ' ')
  AGENT_COUNT=$(ls "${DATA_DIR}/agents" 2>/dev/null | wc -l | tr -d ' ')
  COMMAND_COUNT=$(ls "${DATA_DIR}/commands" 2>/dev/null | wc -l | tr -d ' ')
  echo -e "  ${GREEN}DONE${RESET} Planted ${SKILL_COUNT} skills, ${AGENT_COUNT} agents, ${COMMAND_COUNT} commands (mode: ${MODE})"
fi

# ---------------------------------------------------------------------------
# 8. Print the claude mcp add command
# ---------------------------------------------------------------------------
ENTRY_POINT="$SCRIPT_DIR/dist/index.js"

heading "How to register the MCP server with Claude Code"

printf "\n${CYAN}Option A — claude mcp add (one-time command):${RESET}\n\n"
printf "  ${BOLD}claude mcp add workspace-platform \\\\\n"
printf "    --transport stdio \\\\\n"
printf "    -e PLATFORM_ROOT='%s' \\\\\n" "$PLATFORM_ROOT"
printf "    -e WORKSPACE_ROOT='%s' \\\\\n" "$WORKSPACE_ROOT"
printf "    -- node '%s'${RESET}\n\n" "$ENTRY_POINT"

# ---------------------------------------------------------------------------
# 9. Print the ~/.claude/settings.json snippet
# ---------------------------------------------------------------------------
printf "${CYAN}Option B — add to ~/.claude/settings.json manually:${RESET}\n\n"
cat <<JSON
{
  "mcpServers": {
    "workspace-platform": {
      "command": "node",
      "args": ["${ENTRY_POINT}"],
      "env": {
        "PLATFORM_ROOT": "${PLATFORM_ROOT}",
        "WORKSPACE_ROOT": "${WORKSPACE_ROOT}"
      }
    }
  }
}
JSON

printf "\n${YELLOW}Note:${RESET} If you already have other entries in mcpServers, merge the\n"
printf "      \"workspace-platform\" key into the existing object — don't replace the whole file.\n"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
printf "\n${GREEN}${BOLD}Setup complete.${RESET} Run the claude mcp add command above, then restart\n"
printf "Claude Code. You should see the workspace-platform server listed in\n"
printf "${BOLD}claude mcp list${RESET}.\n\n"
