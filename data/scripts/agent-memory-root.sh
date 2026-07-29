#!/usr/bin/env sh
# agent-memory-root.sh — agent-memory unified-root resolver (E2 / self-improving-tooling-m2, S2.1)
#
# A tiny, sourceable POSIX-sh helper. NO new deps (stdlib/bash only). This is the
# resolution-order helper for NEW code (e.g. the E3 trajectory reader) and for the S2.2
# smoke test's fallback-branch assertion. The EXISTING 59 agent bodies are NOT changed and
# do NOT source this — they are fronted transparently by the symlink-farm that
# agent-memory-consolidate.sh installs. (Editing the 59 bodies to source this is the BLOCKER:
# 59 coordinated edits with a silent empty-read failure mode. This helper avoids that.)
#
# Resolution order is NEW-FIRST / LEGACY-SECOND (S2.1 second acceptance criterion) so no
# cross-repo run silently loses its lessons mid-cutover:
#   1. $AGENT_MEMORY_ROOT                       (explicit override — highest priority)
#   2. $WORKSPACE_ROOT/.claude/agent-memory     (canonical default, only if it exists on disk)
#   3. the legacy cwd-root (default ./.claude/agent-memory) — BACK-COMPAT fallback
#
# The fallback is the orphan-prevention guarantee: if the override is unset AND the canonical
# default is absent, resolution lands on the legacy cwd-root (where the lessons physically are
# pre-migration), NEVER on an empty read. That is the exact failure mode the milestone BLOCKER
# warns about (unset var -> empty read -> agent runs cold) — here it degrades to the legacy
# corpus instead.
#
# Usage:
#   . "$(dirname "$0")/agent-memory-root.sh"
#   dir=$(agent_memory_root milestone-researcher)              # -> <base>/milestone-researcher
#   dir=$(agent_memory_root milestone-researcher ./alt/root)   # custom legacy cwd-root
#   base=$(agent_memory_base)                                  # just the base dir, no agent name
#
# Both functions echo a path on stdout and return 0. They never mutate the filesystem.

# agent_memory_base [legacy-cwd-root]
# Echo the resolved BASE agent-memory dir (no agent-name suffix).
agent_memory_base() {
  _amr_legacy="${1:-.claude/agent-memory}"
  if [ -n "${AGENT_MEMORY_ROOT:-}" ]; then
    # (1) explicit override — highest priority. Honored even if it does not yet exist on disk
    #     (the consolidate migration / mkdir -p creates it); honoring an explicit override
    #     unconditionally is what lets an operator point the farm at a fresh root.
    printf '%s\n' "$AGENT_MEMORY_ROOT"
  elif [ -n "${WORKSPACE_ROOT:-}" ] && [ -d "$WORKSPACE_ROOT/.claude/agent-memory" ]; then
    # (2) canonical default — only when it actually exists, so an unset/empty new root cannot
    #     shadow a populated legacy corpus.
    printf '%s\n' "$WORKSPACE_ROOT/.claude/agent-memory"
  else
    # (3) legacy cwd-root — back-compat fallback (orphan prevention).
    printf '%s\n' "$_amr_legacy"
  fi
}

# agent_memory_root <agent-name> [legacy-cwd-root]
# Echo the resolved lessons DIR for <agent-name> under the resolved base.
agent_memory_root() {
  if [ -z "${1:-}" ]; then
    echo "agent_memory_root: missing <agent-name>" >&2
    return 2
  fi
  _amr_name="$1"
  _amr_base=$(agent_memory_base "${2:-.claude/agent-memory}")
  printf '%s/%s\n' "$_amr_base" "$_amr_name"
}

# When executed directly (not sourced), act as a tiny CLI for the smoke test / ad-hoc use:
#   agent-memory-root.sh <agent-name> [legacy-cwd-root]   -> prints the resolved dir
#   agent-memory-root.sh --base [legacy-cwd-root]          -> prints just the base
if [ "${AMR_SOURCED_ONLY:-}" != "1" ]; then
  case "$0" in
    */agent-memory-root.sh|agent-memory-root.sh)
      case "${1:-}" in
        --base) shift; agent_memory_base "${1:-.claude/agent-memory}" ;;
        "") : ;;  # sourced with no args, or `sh agent-memory-root.sh` with none — no-op
        *)  agent_memory_root "$@" ;;
      esac
      ;;
  esac
fi
