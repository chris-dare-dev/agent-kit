#!/usr/bin/env bash
# Scaffold a new <repo-root>/plans/<slug>-roadmap.md from the template.
# Usage: init-roadmap.sh <slug> "<title or brief>" [--repo-root PATH] [--owner NAME]
#
# Idempotent: if the file already exists, detects which phase to resume and
# prints RESUMING phase=<X> roadmap=<slug>.  Only prints INITIALIZED for a
# genuinely fresh roadmap doc.
#
# Repo-root detection (in order — same as spike-init.sh hardened version):
#   1. --repo-root flag if passed
#   2. $REPO_ROOT env var if set
#   3. $PLATFORM_ROOT env var if set (workspace root)
#   4. `git rev-parse --show-toplevel` from CWD if currently inside a git repo
#   5. EXIT 2 — no walk-up-from-script-location fallback (script lives in MCP
#      server repo; walking up from script dir would land in the wrong tree).
#
# Template: data/references/roadmap-template-roadmap.md (resolved via
# $MCP_SERVER_ROOT env var, then by walking up from REPO_ROOT).

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: init-roadmap.sh <slug> \"<title or brief>\" [--repo-root PATH] [--owner NAME]" >&2
  exit 2
fi

SLUG="$1"
TITLE="$2"
shift 2

REPO_ROOT_OVERRIDE=""
OWNER="${USER:-unknown}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT_OVERRIDE="${2:-}"
      shift 2
      ;;
    --owner)
      OWNER="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# Validate slug (kebab-case, ≤ 40 chars, alphanumeric + hyphen)
if ! [[ "$SLUG" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "invalid slug: '$SLUG'. Must be lowercase kebab-case, ≤ 40 chars, [a-z0-9-]." >&2
  exit 2
fi

# Repo-root detection (same resolution order as spike-init.sh hardened version).
if [[ -n "$REPO_ROOT_OVERRIDE" ]]; then
  REPO_ROOT="$REPO_ROOT_OVERRIDE"
elif [[ -n "${REPO_ROOT:-}" ]]; then
  : # use env var as-is
elif [[ -n "${PLATFORM_ROOT:-}" && -d "${PLATFORM_ROOT:-}" ]]; then
  REPO_ROOT="$PLATFORM_ROOT"
elif REPO_ROOT_FROM_CWD=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$REPO_ROOT_FROM_CWD"
fi
# Deliberately NO walk-up-from-script-location fallback. The script lives in
# the MCP server repo; walking up from script dir would land in the wrong tree.

if [[ -z "${REPO_ROOT:-}" || ! -d "$REPO_ROOT" ]]; then
  echo "could not determine repo root. Pass --repo-root PATH or set REPO_ROOT/PLATFORM_ROOT env var." >&2
  exit 2
fi

PLANS_DIR="$REPO_ROOT/plans"
ROADMAP="$PLANS_DIR/$SLUG-roadmap.md"

if [[ -f "$ROADMAP" ]]; then
  # Resume detection: identify first unpopulated section (Critical Gotcha #9).
  # Phase sections are populated in order: Goal → Epics → Roadmap → Cross-references.
  RESUME_PHASE="complete"
  if ! grep -q "^## Cross-references" "$ROADMAP" 2>/dev/null || \
     [[ $(grep -A5 "^## Cross-references" "$ROADMAP" | wc -c) -lt 60 ]]; then
    RESUME_PHASE="materialize"
  fi
  if ! grep -q "^## Roadmap" "$ROADMAP" 2>/dev/null || \
     [[ $(grep -A5 "^## Roadmap" "$ROADMAP" | wc -c) -lt 60 ]]; then
    RESUME_PHASE="sequence"
  fi
  if ! grep -q "^## Epics" "$ROADMAP" 2>/dev/null || \
     [[ $(grep -A5 "^## Epics" "$ROADMAP" | wc -c) -lt 60 ]]; then
    RESUME_PHASE="decompose"
  fi
  if ! grep -q "^## Goal" "$ROADMAP" 2>/dev/null || \
     [[ $(grep -A5 "^## Goal" "$ROADMAP" | wc -c) -lt 60 ]]; then
    RESUME_PHASE="refine"
  fi
  echo "RESUMING phase=$RESUME_PHASE: $ROADMAP"
  echo "  path: $ROADMAP"
  exit 0
fi

mkdir -p "$PLANS_DIR"

# Ensure Claude pipeline artifacts are gitignored in the target repo (idempotent, non-fatal).
"$(dirname "$0")/ensure-claude-gitignore.sh" "$REPO_ROOT" 2>/dev/null || true

# Template resolution: try MCP_SERVER_ROOT env, then REPO_ROOT tree walk, then
# $0-derived fallback. The $0 fallback is load-bearing for cases where the
# script is invoked from outside the platform monorepo (e.g. `/roadmap` from a
# different workspace) — without it, init.sh fails with "template not found"
# on any non-platform repo.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE=""
if [[ -n "${MCP_SERVER_ROOT:-}" && -f "$MCP_SERVER_ROOT/data/references/roadmap-template-roadmap.md" ]]; then
  TEMPLATE="$MCP_SERVER_ROOT/data/references/roadmap-template-roadmap.md"
elif [[ -f "$REPO_ROOT/agent-kit/data/references/roadmap-template-roadmap.md" ]]; then
  TEMPLATE="$REPO_ROOT/agent-kit/data/references/roadmap-template-roadmap.md"
elif [[ -f "$REPO_ROOT/../agent-kit/data/references/roadmap-template-roadmap.md" ]]; then
  CAND="$REPO_ROOT/../agent-kit/data/references/roadmap-template-roadmap.md"
  TEMPLATE="$(cd "$(dirname "$CAND")" && pwd)/$(basename "$CAND")"
elif [[ -f "$SCRIPT_DIR/../references/roadmap-template-roadmap.md" ]]; then
  # Script lives at <mcp-server>/data/scripts/; $SCRIPT_DIR/../references resolves to data/references/.
  TEMPLATE="$(cd "$SCRIPT_DIR/../references" && pwd)/roadmap-template-roadmap.md"
else
  echo "template not found. Set MCP_SERVER_ROOT to the agent-kit directory, or ensure data/references/roadmap-template-roadmap.md is resolvable from \$0 or REPO_ROOT." >&2
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "template not found at $TEMPLATE" >&2
  exit 1
fi

NOW=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")

# Substitute {{TITLE}}, {{SLUG}}, {{CREATED_AT}}, {{OWNER}}, {{BRIEF}} in the template.
# Use python for the substitution to handle multi-line BRIEF safely.
python3 - "$TEMPLATE" "$ROADMAP" "$SLUG" "$TITLE" "$NOW" "$OWNER" <<'PY'
import sys
template_path, out_path, slug, title, now, owner = sys.argv[1:7]
text = open(template_path).read()
# {{BRIEF}} is the full title for now; user expands during Phase 1.
substitutions = {
    "{{TITLE}}": title,
    "{{SLUG}}": slug,
    "{{CREATED_AT}}": now,
    "{{OWNER}}": owner,
    "{{BRIEF}}": title,
}
for k, v in substitutions.items():
    text = text.replace(k, v)
with open(out_path, "w") as f:
    f.write(text)
PY

echo "INITIALIZED: $ROADMAP"
echo "  repo_root: $REPO_ROOT"
echo "  slug:      $SLUG"
echo "  title:     $TITLE"
echo "  owner:     $OWNER"
echo "  created:   $NOW"
echo ""
echo "Next: Phase 1 (REFINE) populates the ## Goal section."
