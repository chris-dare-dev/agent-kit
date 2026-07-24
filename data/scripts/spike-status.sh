#!/usr/bin/env bash
#
# spike-status.sh — list all spikes, or show one spike's detail, FROM state.json.
#
# Usage:
#   spike-status.sh                 # table of all spikes
#   spike-status.sh <spike-id>      # detail view for one spike
#   spike-status.sh [--repo-root PATH]
#
# spike v2: phase / attempt / verdict / review-verdict / terminal all come from
# state.json (the machine authority) — NOT inferred from which artifact files
# exist. A half-written note.md no longer reads as "phase complete".
#
# Repo-root detection: --repo-root, $REPO_ROOT, $PLATFORM_ROOT,
# `git rev-parse --show-toplevel`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SPIKE_ID=""
REPO_ROOT_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) REPO_ROOT_OVERRIDE="${2:-}"; shift 2 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)
      [[ -n "$SPIKE_ID" ]] && { echo "unexpected extra argument: $1" >&2; exit 2; }
      SPIKE_ID="$1"; shift ;;
  esac
done

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

SPIKES_DIR="$REPO_ROOT/.claude/notes/spikes"
LOCK_FILE="$SPIKES_DIR/.lock"

# state_field <state.json> <field> — print one field, "" if absent/unreadable.
state_field() {
  local sp="$1" field="$2"
  [[ -f "$sp" ]] || { echo ""; return; }
  python3 - "$sp" "$field" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    v = d.get(sys.argv[2])
    print("" if v is None else v)
except Exception:
    print("")
PY
}

lock_status() {
  if [[ ! -f "$LOCK_FILE" ]]; then
    echo "(unlocked)"
  else
    local content id ts
    content=$(cat "$LOCK_FILE")
    id=$(echo "$content" | cut -d: -f2)
    ts=$(echo "$content" | cut -d: -f3-)
    echo "LOCKED by $id (since $ts) — release with spike-release.sh $id"
  fi
}

show_all() {
  if [[ ! -d "$SPIKES_DIR" ]]; then
    echo "No spikes directory found at $SPIKES_DIR"
    echo "Run /spike <id> to start a spike."
    exit 0
  fi
  echo "Lock:  $(lock_status)"
  echo ""
  local found=0
  while IFS= read -r -d '' spike_dir; do
    local id sp phase attempt verdict review terminal
    id=$(basename "$spike_dir")
    [[ "$id" == .* ]] && continue
    [[ ! -d "$spike_dir" ]] && continue
    sp="$spike_dir/state.json"
    phase=$(state_field "$sp" phase); phase="${phase:-(no state.json)}"
    attempt=$(state_field "$sp" attempt); attempt="${attempt:-–}"
    verdict=$(state_field "$sp" verdict); verdict="${verdict:-–}"
    review=$(state_field "$sp" review_verdict); review="${review:-–}"
    terminal=$(state_field "$sp" terminal_status)
    [[ -n "$terminal" ]] && phase="$phase/$terminal"
    if [[ "$found" -eq 0 ]]; then
      printf '%-38s %-20s %-4s %-11s %s\n' "SPIKE-ID" "PHASE" "ATT" "VERDICT" "REVIEW"
      printf '%-38s %-20s %-4s %-11s %s\n' "--------" "-----" "---" "-------" "------"
    fi
    found=1
    printf '%-38s %-20s %-4s %-11s %s\n' "$id" "$phase" "$attempt" "$verdict" "$review"
  done < <(find "$SPIKES_DIR" -maxdepth 1 -mindepth 1 -type d -print0 2>/dev/null | sort -z)
  if [[ "$found" -eq 0 ]]; then
    echo "No spikes found in $SPIKES_DIR"
    echo "Run /spike <id> to start a spike."
  fi
}

show_one() {
  local id="$1" dir="$SPIKES_DIR/$1" sp="$SPIKES_DIR/$1/state.json"
  if [[ ! -d "$dir" ]]; then
    echo "spike '$id' not found at $dir" >&2
    exit 1
  fi
  echo "Spike:            $id"
  if [[ ! -f "$sp" ]]; then
    echo "  (no state.json — pre-v2 or uninitialized; run /spike $id)"
    echo "Lock:             $(lock_status)"
    return
  fi
  echo "Phase:            $(state_field "$sp" phase)"
  echo "Attempt:          $(state_field "$sp" attempt)"
  echo "Loop counts:      rerun=$(state_field "$sp" rerun_count) reconsider=$(state_field "$sp" reconsider_count) deviation=$(state_field "$sp" deviation_count)"
  local terminal
  terminal=$(state_field "$sp" terminal_status)
  echo "Terminal status:  ${terminal:-(not terminal)}"
  echo ""
  echo "Verdict (derived, decision.json):  $(state_field "$sp" verdict)"
  echo "Review verdict (review.json):      $(state_field "$sp" review_verdict)"
  echo "Skipped review:   $(state_field "$sp" skipped_review)"
  echo "Outcome emitted:  $(state_field "$sp" outcome_emitted)"
  echo ""
  echo "Artifacts:"
  for artifact in design.json design.md measurements.json decision.json note.md review.json review.md design-deviation.md; do
    if [[ -f "$dir/$artifact" ]]; then
      printf '  %-24s present\n' "$artifact"
    else
      printf '  %-24s (absent)\n' "$artifact"
    fi
  done
  if [[ -d "$dir/poc" ]]; then
    local poc_count
    poc_count=$(find "$dir/poc" -maxdepth 3 -type f 2>/dev/null | wc -l | tr -d ' ')
    printf '  %-24s %s file(s)\n' "poc/" "$poc_count"
  fi
  echo ""
  echo "Lock:             $(lock_status)"
}

if [[ -n "$SPIKE_ID" ]]; then
  show_one "$SPIKE_ID"
else
  show_all
fi
