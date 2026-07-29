#!/usr/bin/env bash
#
# spike-init.sh — bootstrap a spike, acquire the coarse orchestrator lock, and
# create the machine state (state.json) via spike-checkpoint.py.
#
# Usage:
#   spike-init.sh <spike-id> [--repo-root PATH] [--roadmap-path PATH]
#
# TWO locks, deliberately separate:
#   1. The COARSE orchestrator lock at .claude/notes/spikes/.lock — "one spike at
#      a time, system-wide". Acquired here with an ATOMIC O_EXCL create (set -C).
#      There is NO PID-liveness auto-clear: the pre-v2 lock stored this script's
#      own $$ (dead the instant the script returned), so the next invocation's
#      `kill -0` always saw a dead PID and silently cleared the lock — it never
#      actually gated anything. A crashed run now leaves the lock held until an
#      explicit `spike-release.sh <id>` (or `--force`), exactly as documented.
#   2. The per-run state lock at state.json.lock — an fcntl lock held only for
#      each read-modify-write inside spike-checkpoint.py. Not this script's job.
#
# Resume is STATE-DRIVEN (state.json phase), never inferred from which files
# happen to exist — a zero-byte or half-written artifact can no longer read as
# "phase complete".
#
# Repo-root detection (in order): --repo-root, $REPO_ROOT, $PLATFORM_ROOT,
# `git rev-parse --show-toplevel`. No walk-up-from-script fallback (it would land
# in the MCP-server repo, not the workspace).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VALIDATE_SCRIPT="$SCRIPT_DIR/validate-spike-id.sh"
CHECKPOINT="$SCRIPT_DIR/spike-checkpoint.py"

if [[ $# -lt 1 ]]; then
  echo "usage: spike-init.sh <spike-id> [--repo-root PATH] [--roadmap-path PATH]" >&2
  exit 2
fi

ID="$1"; shift

REPO_ROOT_OVERRIDE=""
ROADMAP_PATH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)     REPO_ROOT_OVERRIDE="${2:-}"; shift 2 ;;
    --roadmap-path)  ROADMAP_PATH="${2:-}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Validate spike id shape.
if [[ -x "$VALIDATE_SCRIPT" ]]; then
  "$VALIDATE_SCRIPT" "$ID"
else
  echo "WARN: validate-spike-id.sh not found — skipping shape check" >&2
fi

# Repo-root detection.
if [[ -n "$REPO_ROOT_OVERRIDE" ]]; then
  REPO_ROOT="$REPO_ROOT_OVERRIDE"
elif [[ -n "${REPO_ROOT:-}" ]]; then
  :
elif [[ -n "${PLATFORM_ROOT:-}" && -d "${PLATFORM_ROOT:-}" ]]; then
  REPO_ROOT="$PLATFORM_ROOT"
elif REPO_ROOT_FROM_CWD=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$REPO_ROOT_FROM_CWD"
fi
if [[ -z "${REPO_ROOT:-}" || ! -d "$REPO_ROOT" ]]; then
  echo "could not determine repo root. Pass --repo-root PATH or set REPO_ROOT/PLATFORM_ROOT." >&2
  exit 2
fi
# Export so spike-checkpoint.py resolves the SAME root.
export REPO_ROOT

SPIKES_DIR="$REPO_ROOT/.claude/notes/spikes"
LOCK_FILE="$SPIKES_DIR/.lock"

# Lock format (field 1 = informational marker, NOT used for liveness; keep 3
# colon-fields so release.sh/status.sh `cut -d: -f2` still reads the id):
#   <marker>:<spike-id>:<iso-utc>
mkdir -p "$SPIKES_DIR"

# Ensure Claude pipeline artifacts are gitignored (idempotent, non-fatal).
"$SCRIPT_DIR/ensure-claude-gitignore.sh" "$REPO_ROOT" 2>/dev/null || true

# ---- Acquire the coarse lock atomically (O_EXCL via noclobber) --------------
NOW_ISO=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
MARKER="spike"   # informational only; liveness is NOT PID-derived
if ( set -o noclobber; printf '%s:%s:%s' "$MARKER" "$ID" "$NOW_ISO" > "$LOCK_FILE" ) 2>/dev/null; then
  : # acquired a fresh lock
else
  # Lock already exists — read the holder id.
  LOCK_ID=$(cut -d: -f2 "$LOCK_FILE" 2>/dev/null || echo "")
  if [[ "$LOCK_ID" != "$ID" ]]; then
    echo "spike orchestrator is locked by '$LOCK_ID' (lock: $(cat "$LOCK_FILE" 2>/dev/null))" >&2
    echo "One spike runs at a time. If it finished/crashed, run:" >&2
    echo "  bash \"$SCRIPT_DIR/spike-release.sh\" $LOCK_ID          # release the holder" >&2
    echo "  bash \"$SCRIPT_DIR/spike-release.sh\" $ID --force        # steal the lock" >&2
    exit 1
  fi
  # Held by THIS spike already — idempotent resume, keep the existing lock.
fi

# ---- Create or inspect machine state ----------------------------------------
INIT_ARGS=("$ID" "--init")
[[ -n "$ROADMAP_PATH" ]] && INIT_ARGS+=("--roadmap-path" "$ROADMAP_PATH")
INIT_OUT=$(python3 "$CHECKPOINT" "${INIT_ARGS[@]}")

if echo "$INIT_OUT" | grep -q "already exists"; then
  PHASE=$(python3 "$CHECKPOINT" "$ID" --get phase)
  ATTEMPT=$(python3 "$CHECKPOINT" "$ID" --get attempt)
  TERMINAL=$(python3 "$CHECKPOINT" "$ID" --get terminal_status)
  if [[ -n "$TERMINAL" ]]; then
    echo "RESUMING spike=$ID phase=$PHASE attempt=$ATTEMPT terminal=$TERMINAL"
    echo "  (already terminal — release the lock with spike-release.sh if it is still held)"
  else
    echo "RESUMING spike=$ID phase=$PHASE attempt=$ATTEMPT"
  fi
  echo "  state: $SPIKES_DIR/$ID/state.json"
  echo "  lock:  $LOCK_FILE"
else
  echo "INITIALIZED spike=$ID"
  echo "  state: $SPIKES_DIR/$ID/state.json"
  echo "  lock:  $LOCK_FILE"
fi
