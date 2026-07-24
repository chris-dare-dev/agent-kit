#!/usr/bin/env bash
# ensure-claude-gitignore.sh — idempotently ensure a repo's LOCAL git excludes
# cover Claude Code multi-agent pipeline artifacts (notes, agent-memory,
# critiques, findings registers).
#
# Why: the pipeline init scripts create .claude/notes/<...>/ in the target repo,
# and subagents later write .claude/agent-memory/<name>/lessons.md plus
# docs/<id>_<critic>_critique.md. None of these are meant to be committed.
#
# TARGET: .git/info/exclude — NOT the tracked .gitignore (policy decision,
# Chris Dare 2026-07-09, round-4 proc-9). Identical ignore semantics, but
# per-clone and untracked: appending to a repo's tracked .gitignore silently
# dirtied every pipeline-touched repo's working tree until someone committed a
# tooling concern the repo never asked for. Repos whose .gitignore ALREADY
# carries the block (committed before this change) are left alone — the marker
# check treats either location as satisfied.
#
# Usage: ensure-claude-gitignore.sh <REPO_ROOT>
# Idempotent: the block is appended only if its marker is absent from BOTH the
# tracked .gitignore and .git/info/exclude. Never commits, never edits tracked
# files.
set -euo pipefail

REPO_ROOT="${1:-}"
if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT" ]]; then
  echo "ensure-claude-gitignore: REPO_ROOT missing or not a dir: '$REPO_ROOT'" >&2
  exit 0   # non-fatal: never block a pipeline on gitignore hygiene
fi

MARKER="Claude Code multi-agent pipeline artifacts"

# Legacy location: a previously-committed block in the tracked .gitignore
# still protects the repo — do not duplicate it into the exclude file.
GI="$REPO_ROOT/.gitignore"
if [[ -f "$GI" ]] && grep -qF "$MARKER" "$GI"; then
  exit 0
fi

# Canonical location: per-clone, untracked. Resolve the real git dir (worktrees
# and submodules have a .git FILE pointing elsewhere).
GIT_DIR="$(git -C "$REPO_ROOT" rev-parse --git-dir 2>/dev/null || true)"
if [[ -z "$GIT_DIR" ]]; then
  echo "ensure-claude-gitignore: '$REPO_ROOT' is not a git repo — skipping" >&2
  exit 0   # non-fatal, as above
fi
[[ "$GIT_DIR" = /* ]] || GIT_DIR="$REPO_ROOT/$GIT_DIR"
EXCLUDE="$GIT_DIR/info/exclude"

if [[ -f "$EXCLUDE" ]] && grep -qF "$MARKER" "$EXCLUDE"; then
  exit 0
fi

mkdir -p "$(dirname "$EXCLUDE")"
cat >> "$EXCLUDE" <<'EOF'

# === Claude Code multi-agent pipeline artifacts — local-only, do not commit ===
.claude/notes/
.claude/agent-memory/
.claude/settings.local.json
*_adversary_critique.md
*_infra_critique.md
*_frontend_critique.md
EOF

echo "ensure-claude-gitignore: added pipeline-artifact block to ${EXCLUDE}" >&2
