#!/usr/bin/env bash
# 15-min LIVE backstop: regenerate every project's roadmap status board so checkbox flips made
# OUTSIDE Claude Code (or missed by the hook) surface. Boards are status-sig'd → a no-op when
# nothing changed (no churn). The PostToolUse hook already updates a board instantly on agent edits.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${PERSONAL_WORKSPACE_ROOT:-${PERSONAL_SOURCE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}}"
LOG="$DIR/.claude/notes/vault-restructure/board-refresh.log"
LINTLOG="$DIR/.claude/notes/vault-restructure/roadmap-status-lint.log"
{
  echo "=== board refresh $(date '+%Y-%m-%d %H:%M:%S') ==="
  python3 "$DIR/scripts/roadmap_status_excalidraw.py" --reconcile
} >> "$LOG" 2>&1

# Drift guard (non-fatal): flag any milestone authored WITHOUT a status line — it silently
# renders as `pending` and undercounts the board. Overwrite each run so the log is current state.
{
  echo "=== roadmap-status lint $(date '+%Y-%m-%d %H:%M:%S') ==="
  python3 "$DIR/scripts/roadmap_status_excalidraw.py" --lint-all
} > "$LINTLOG" 2>&1 || true
