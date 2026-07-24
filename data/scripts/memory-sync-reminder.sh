#!/usr/bin/env bash
# PostToolUse reminder hook for the memory-write protocol.
#
# Fires ONLY when an Edit/Write touches a durable-memory surface:
#   - a per-repo agent-memory lessons file (.../.claude/agent-memory/<agent>/lessons.md)
#   - a source-repo CLAUDE.md (.../platform/source/<repo>/CLAUDE.md)
# It emits a single additionalContext nudge to consider `/memory-sync` so durable,
# cross-agent lessons get promoted to the repo wiki Field Notes instead of dying in a
# local, gitignored lessons.md. It NEVER blocks, mutates, or writes externally — it is a
# pure reminder. Wire via .claude/settings.json PostToolUse (matcher "Edit|Write").
#
# Input: the PostToolUse JSON payload on stdin. Output: hookSpecificOutput JSON (or nothing).
set -u

input="$(cat)"
fp="$(printf '%s' "$input" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("tool_input") or {}).get("file_path", ""))
except Exception:
    print("")' 2>/dev/null)"

[ -z "$fp" ] && exit 0

case "$fp" in
  */.claude/agent-memory/*/lessons.md | */platform/source/*/CLAUDE.md)
    cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"[memory-sync] You edited a durable-memory file. If this lesson is significant AND useful to OTHER agents in this repo, promote it per the memory-write protocol — consider /memory-sync (capture -> wiki Field Notes -> CLAUDE.md -> data/references; promote, do not duplicate across tiers). Routine per-run notes can stay in lessons.md."}}
JSON
    ;;
  *)
    : # not a memory surface — stay silent
    ;;
esac
exit 0
