#!/usr/bin/env bash
# setup-local.sh — thin compatibility wrapper over `agent-kit init`.
#
# This was a 400-line installer that could not run in this repository: it walked
# parent directories for one containing both charts/ and tools/, then for one
# containing GitLab/, and exited otherwise — so on any ordinary clone it failed
# before npm ci, and even --build-only was unreachable. Its `while [[ "$dir" !=
# "/" ]]` walk also never terminated on a Windows path root.
#
# The real installer is now scripts/init.mjs, in plain Node, so one `node >= 20`
# prerequisite covers setup on macOS, Linux and Windows alike. This file exists
# only so the documented command keeps working for anyone who bookmarked it.
#
#   ./setup-local.sh [--build-only] [--force] [--dry-run] [--mode symlink|copy]
#
# Every argument is forwarded unchanged. On Windows use the Node entry point
# directly — this wrapper needs bash:
#
#   node scripts/cli.mjs init
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "setup-local.sh: node is required (>= 20) — https://nodejs.org" >&2
  exit 1
fi

echo "setup-local.sh: delegating to \`agent-kit init\` (scripts/init.mjs)" >&2
exec node "$here/scripts/init.mjs" "$@"
