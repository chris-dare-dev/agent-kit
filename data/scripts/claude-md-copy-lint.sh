#!/usr/bin/env bash
#
# claude-md-copy-lint.sh — fail if any data/claude-md/ copy has drifted from its
# active workspace counterpart.
#
# WHY: skills/agents/references/scripts are SYMLINKS from .claude/ into data/, so a
# single git-tracked file is edited from either path (zero drift). The workspace-tier
# CLAUDE.md / AGENTS.md files are the ONE exception — they are real-file COPIES
# (CLAUDE.md auto-loads by natural directory path and cannot be a symlink into data/),
# kept byte-identical only by the manual "edit both, diff" discipline. That honor
# system has already failed in the wild (a worktree CLAUDE.md drifted ~17 KB). This
# script turns the honor system into an enforced gate.
#
# Run manually, or wire as a git pre-commit hook in the WORKSPACE (where both copies
# exist). It cannot run in the agent-kit repo's own CI — the active copies
# live outside this repo (workspace root / sibling repos), so only a full-workspace
# checkout can compare them.
#
# FRONTMATTER: the data/ copies may carry a leading Obsidian YAML frontmatter block
# (`---` ... `---` starting at line 1) that the active copies deliberately lack
# (CLAUDE.md files are auto-loaded prompt context; frontmatter would pollute it).
# The lint therefore strips a leading frontmatter block from BOTH sides before
# diffing — the BODY below the frontmatter is what must be byte-identical.
#
# Env: WORKSPACE_ROOT and PLATFORM_ROOT override the auto-derived paths.
# Exit: 0 = all copies in sync (or active copy absent → skipped); 1 = drift found.

set -euo pipefail

# Emit a file's content minus a LEADING YAML frontmatter block, if one exists.
# A frontmatter block = line 1 is exactly `---` and a later line is exactly `---`.
# If line 1 is `---` but no closing delimiter exists, the file is emitted unchanged
# (malformed frontmatter is a content difference, not something to hide).
strip_frontmatter() {
  local f="$1" end
  if [[ "$(head -n 1 "$f")" == "---" ]]; then
    end="$(awk 'NR > 1 && $0 == "---" { print NR; exit }' "$f")"
    if [[ -n "${end}" ]]; then
      tail -n +"$((end + 1))" "$f"
      return
    fi
  fi
  cat "$f"
}

# --- Resolve roots --------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# data/scripts/ -> agent-kit -> tools -> platform
DEFAULT_PLATFORM_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
PLATFORM_ROOT="${PLATFORM_ROOT:-${DEFAULT_PLATFORM_ROOT}}"
# platform -> <group> -> GitLab -> workspace root
DEFAULT_WORKSPACE_ROOT="$(cd "${PLATFORM_ROOT}/../../.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${DEFAULT_WORKSPACE_ROOT}}"

DATA_CLAUDE_MD="${SCRIPT_DIR}/../claude-md"

# --- The four copy pairs: "<data/ copy>|<active workspace path>" -----------------
PAIRS=(
  "${DATA_CLAUDE_MD}/workspace-root.md|${WORKSPACE_ROOT}/CLAUDE.md"
  "${DATA_CLAUDE_MD}/gitlab-domain.md|${WORKSPACE_ROOT}/repos/CLAUDE.md"
  "${DATA_CLAUDE_MD}/platform-monorepo.md|${PLATFORM_ROOT}/CLAUDE.md"
  "${DATA_CLAUDE_MD}/AGENTS.md|${WORKSPACE_ROOT}/AGENTS.md"
  "${DATA_CLAUDE_MD}/CONTEXT.md|${WORKSPACE_ROOT}/CONTEXT.md"
)

drift=0
checked=0
skipped=0

for pair in "${PAIRS[@]}"; do
  canonical="${pair%%|*}"
  active="${pair##*|}"

  if [[ ! -f "${canonical}" ]]; then
    echo "SKIP (data/ copy missing): ${canonical}"
    skipped=$((skipped + 1))
    continue
  fi
  if [[ ! -f "${active}" ]]; then
    # Not a full workspace (e.g. CI of the server repo alone) — nothing to compare.
    echo "SKIP (active copy absent — not a full workspace): ${active}"
    skipped=$((skipped + 1))
    continue
  fi

  if diff -q <(strip_frontmatter "${canonical}") <(strip_frontmatter "${active}") >/dev/null 2>&1; then
    echo "OK    ${canonical#"${PLATFORM_ROOT}/"}  ==  ${active}"
    checked=$((checked + 1))
  else
    echo "DRIFT ${canonical}  !=  ${active}"
    echo "      → reconcile (bodies below any leading YAML frontmatter must be byte-identical):"
    echo "        diff '${canonical}' '${active}'"
    echo "        (a leading '---' YAML frontmatter block on the data/ copy is expected — ignore that hunk)"
    drift=$((drift + 1))
  fi
done

echo "---"
echo "checked=${checked} drift=${drift} skipped=${skipped}"
if [[ "${drift}" -gt 0 ]]; then
  echo "FAIL: ${drift} claude-md copy/copies drifted. Edit BOTH paths to match, then re-run."
  exit 1
fi
echo "PASS: all comparable claude-md copies are body-identical (frontmatter excluded)."
