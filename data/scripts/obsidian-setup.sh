#!/usr/bin/env bash
# obsidian-setup.sh — Register the Obsidian git clean filter for this platform repo clone.
#
# Run once after cloning (or whenever the repo moves on disk):
#   bash tools/claude-mcp-server/data/scripts/obsidian-setup.sh
#
# What this does:
#   • Registers filter.obsidian-clean in THIS repo's local .git/config
#   • The clean filter strips volatile Obsidian fields (modified, created, cssclass…)
#     before staging *.md files — keeps those auto-managed timestamps out of git
#   • The smudge filter is a no-op (cat) — files on disk keep their Obsidian metadata
#
# After setup, the filter triggers automatically on `git add *.md`.
# Remove with: git config --local --remove-section filter.obsidian-clean
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: must run from inside the platform git repo" >&2
  exit 1
}

FILTER_SCRIPT="$REPO_ROOT/data/scripts/obsidian-clean-filter.py"

if [[ ! -f "$FILTER_SCRIPT" ]]; then
  echo "ERROR: filter script not found at $FILTER_SCRIPT" >&2
  exit 1
fi

PYTHON=$(command -v python3 2>/dev/null) || {
  echo "ERROR: python3 not found in PATH" >&2
  exit 1
}

git config --local filter.obsidian-clean.clean  "$PYTHON '$FILTER_SCRIPT'"
git config --local filter.obsidian-clean.smudge "cat"
git config --local filter.obsidian-clean.required "false"

echo "✓ Registered filter.obsidian-clean in $(git rev-parse --git-dir)/config"
echo "  clean  → $PYTHON '$FILTER_SCRIPT'"
echo "  smudge → cat"
echo ""
echo "The filter runs automatically on 'git add *.md' for files matched by .gitattributes."
echo "To verify: echo 'modified: 2026-06-19' | $PYTHON '$FILTER_SCRIPT'"
