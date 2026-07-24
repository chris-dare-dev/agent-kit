#!/usr/bin/env bash
#
# spike-release.sh — release the coarse orchestrator lock at .claude/notes/spikes/.lock.
#
# Usage:
#   spike-release.sh <spike-id>            # release iff the lock is held by <spike-id>
#   spike-release.sh <spike-id> --force    # release even on holder-id mismatch
#
# Exit: 0 released or nothing-to-release (both success); 1 held by another id
# (without --force); 2 bad args.
#
# SCOPE (changed in spike v2): this releases the LOCK ONLY. The pipeline-outcome
# emit is NOT done here anymore — it is owned by spike-checkpoint.py at the
# terminal (advance to 'complete', or `--terminal <status>`), guarded by
# state.json's outcome_emitted so it fires exactly once. Doing it in both places
# would double-count the training corpus. The orchestrator therefore calls
# `spike-checkpoint.py <id> --terminal <status>` (or advances to 'complete')
# FIRST, then this script to drop the lock.
#
# Repo-root detection (in order): $REPO_ROOT, $PLATFORM_ROOT,
# `git rev-parse --show-toplevel`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VALIDATE_SCRIPT="$SCRIPT_DIR/validate-spike-id.sh"

if [[ $# -lt 1 ]]; then
  echo "usage: spike-release.sh <spike-id> [--force]" >&2
  exit 2
fi

ID="$1"; shift
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -x "$VALIDATE_SCRIPT" ]]; then
  "$VALIDATE_SCRIPT" "$ID"
else
  echo "WARN: validate-spike-id.sh not found — skipping shape check" >&2
fi

if [[ -n "${REPO_ROOT:-}" ]]; then
  :
elif [[ -n "${PLATFORM_ROOT:-}" && -d "${PLATFORM_ROOT:-}" ]]; then
  REPO_ROOT="$PLATFORM_ROOT"
elif REPO_ROOT_FROM_CWD=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$REPO_ROOT_FROM_CWD"
fi
if [[ -z "${REPO_ROOT:-}" || ! -d "$REPO_ROOT" ]]; then
  echo "could not determine repo root. Set REPO_ROOT or PLATFORM_ROOT." >&2
  exit 2
fi

LOCK_FILE="$REPO_ROOT/.claude/notes/spikes/.lock"

if [[ ! -f "$LOCK_FILE" ]]; then
  echo "no lock to release"
  exit 0
fi

LOCK_CONTENT=$(cat "$LOCK_FILE")
LOCK_ID=$(echo "$LOCK_CONTENT" | cut -d: -f2)

if [[ "$LOCK_ID" == "$ID" ]]; then
  rm -f "$LOCK_FILE"
  echo "released lock for spike $ID"
  exit 0
fi

if [[ "$FORCE" -eq 1 ]]; then
  rm -f "$LOCK_FILE"
  echo "WARN: force-released lock held by '$LOCK_ID' on behalf of '$ID'"
  exit 0
fi

echo "ERROR: lock is held by spike '$LOCK_ID', not '$ID'" >&2
echo "Lock content: $LOCK_CONTENT" >&2
echo "  release the holder:  spike-release.sh $LOCK_ID" >&2
echo "  or steal the lock:   spike-release.sh $ID --force" >&2
exit 1
