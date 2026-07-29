#!/usr/bin/env bash
# PostToolUse hook (Edit|Write): when an agent writes a .md that belongs to a tracked
# project, refresh that project's symlinks + _index.md hub. Targeted + fast + NEVER blocks.
#
# Reads the hook payload from stdin (tool_input.file_path); falls back to $TOOL_INPUT.
# Runs the linker in --file mode (one project re-sync) detached so it never slows the agent.
# Always exits 0 — a linker error must never fail a write.

INPUT="$(cat 2>/dev/null)"
FILE="$(printf '%s' "$INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get("tool_input", {}) or {}
    print(ti.get("file_path", "") or "")
except Exception:
    print("")
' 2>/dev/null)"

# Fallback for older hook interfaces that expose $TOOL_INPUT
if [ -z "$FILE" ] && [ -n "$TOOL_INPUT" ]; then
  FILE="$(printf '%s' "$TOOL_INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin); print(d.get("file_path","") or "")
except Exception:
    print("")
' 2>/dev/null)"
  [ -z "$FILE" ] && FILE="$TOOL_INPUT"
fi

case "$FILE" in
  *.md)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    workspace_default="$(cd "$SCRIPT_DIR/.." && pwd -P)"
    DIR="${PERSONAL_WORKSPACE_ROOT:-${PERSONAL_SOURCE_ROOT:-${CLAUDE_PROJECT_DIR:-$workspace_default}}}"
    # Stamp authorship/fix-frontmatter FIRST (self-guards to agent regions only),
    # then re-sync the project's symlinks + hub. Both detached; never block the write.
    nohup sh -c "python3 \"$DIR/scripts/frontmatter-stamp.py\" --file \"$FILE\"; python3 \"$DIR/scripts/project-linker.py\" --file \"$FILE\"" >/dev/null 2>&1 &
    ;;
esac
exit 0
