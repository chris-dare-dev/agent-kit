#!/usr/bin/env bash
# Periodic backstop (launchd): catches .md created OUTSIDE Claude Code (CI, other tools) and
# re-stamps frontmatter. Project linking runs in its default no-delete mode: stale, broken, and
# wrong-target presentation entries are preserved. The PostToolUse hook covers Claude Code writes.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${PERSONAL_WORKSPACE_ROOT:-${PERSONAL_SOURCE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}}"
LOG="$DIR/.claude/notes/vault-restructure/reconcile.log"
{
  echo "=== reconcile $(date '+%Y-%m-%d %H:%M:%S') ==="
  python3 "$DIR/scripts/frontmatter-stamp.py"
  python3 "$DIR/scripts/project-linker.py" --reconcile | grep -E '^==|PRESERVE|SKIP|reference' || true
  # Read-only gate: a source claimed by two projects is reported HERE, at projection time,
  # rather than later as the vault validator's DUPLICATE_MARKDOWN_ALIAS. Never fails the run.
  python3 "$DIR/scripts/project-linker.py" --check-ownership || true
} >> "$LOG" 2>&1
