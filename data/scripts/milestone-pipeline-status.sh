#!/usr/bin/env bash
# Print current milestone-pipeline state in a human-readable form.
# Usage: status.sh <milestone-id> [--repo-root PATH]
#
# Repo-root detection (in order):
#   1. --repo-root flag if passed
#   2. $REPO_ROOT env var if set
#   3. `git rev-parse --show-toplevel` from CWD if currently inside a git repo
#   4. Walk up from this script's dir to nearest .git/

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: status.sh <milestone-id> [--repo-root PATH]" >&2
  exit 2
fi

ID="$1"
shift

REPO_ROOT_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT_OVERRIDE="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -n "$REPO_ROOT_OVERRIDE" ]]; then
  REPO_ROOT="$REPO_ROOT_OVERRIDE"
elif [[ -n "${REPO_ROOT:-}" ]]; then
  : # use env var as-is
elif REPO_ROOT_FROM_CWD=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$REPO_ROOT_FROM_CWD"
else
  REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || true)"
fi

if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT" ]]; then
  echo "could not determine repo root. Pass --repo-root PATH or set REPO_ROOT env var." >&2
  exit 2
fi

STATE="$REPO_ROOT/.claude/notes/milestones/$ID/state.json"

if [[ ! -f "$STATE" ]]; then
  echo "no state for $ID at $STATE -- run init-state.sh first" >&2
  exit 1
fi

python3 - "$STATE" "$(cd "$(dirname "$0")" && pwd)/milestone-pipeline-artifacts.py" <<'PY'
import hashlib, json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

state_path = sys.argv[1]
artifact_script = sys.argv[2]
state = json.load(open(state_path))

if state.get("schema_version") != 2:
    raise SystemExit(
        "state is v1/unversioned — run milestone-pipeline-migrate.py explicitly; "
        "v1 complete is not operational proof"
    )

def parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

now = datetime.now(timezone.utc)
hist = state["phase_history"]

print(f"Milestone: {state['id']}")
print("Schema:    delivery-state v2")
cur_phase = state["phase"]
last_ts = parse(hist[-1]["at"])
mins_in_phase = int((now - last_ts).total_seconds() // 60)
print(f"Phase:     {cur_phase} (since {hist[-1]['at']}, {mins_in_phase} min ago)")

print("History:")
for i, entry in enumerate(hist):
    ts = parse(entry["at"])
    if i + 1 < len(hist):
        nxt = parse(hist[i + 1]["at"])
        delta = nxt - ts
        mins = int(delta.total_seconds() // 60)
        secs = int(delta.total_seconds() % 60)
        if mins > 0:
            elapsed = f"+{mins:>2}m -> {hist[i + 1]['phase']}"
        else:
            elapsed = f"+{secs:>2}s -> {hist[i + 1]['phase']}"
    else:
        elapsed = "(now)"
    print(f"  {entry['phase']:<22} {entry['at']} {elapsed}")

if state.get("research_mode"):
    print(f"Research mode: {state['research_mode']}")
if state.get("implementation_path"):
    line = f"Implementation: {state['implementation_path']}"
    if state.get("implementation_specialist"):
        line += f" (specialist: {state['implementation_specialist']})"
    if state.get("implementation_branch"):
        line += f" on {state['implementation_branch']}"
    print(line)
if state.get("critics_run"):
    print(f"Critics run: {', '.join(state['critics_run'])}")
counts = state.get("critique_finding_counts") or {}
if any(counts.values()):
    parts = " ".join(f"{k[0].upper()}{counts.get(k, 0)}" for k in ("critical", "high", "medium", "low"))
    print(f"Findings:    {parts}")

print(f"Implementation snapshot: {state.get('implementation_status', 'unknown')}")
print(f"Operations snapshot:     {state.get('operational_status', 'unknown')}")
print(f"Review snapshot:         {state.get('review_status', 'unknown')}")
print(
    "Requirements:   publication="
    + ("required" if state.get("publication_required") else "not-required")
    + ", operations="
    + ("required" if state.get("operations_required") else "not-required")
)
bindings = state.get("artifact_bindings") or {}
if bindings:
    print("Bound artifacts:")
    for name, receipt in sorted(bindings.items()):
        digest = str(receipt.get("sha256", ""))[:12]
        print(f"  {name:<26} generation={receipt.get('generation')} sha256={digest} phase={receipt.get('phase')}")

attempts = state.get("check_run_attempts") or []
active_checks = state.get("check_run_hashes") or {}
if attempts:
    print(f"Check attempts: {len(attempts)} total, {len(active_checks)} active passing receipt(s)")

review_path = state.get("review_manifest")
if isinstance(review_path, str):
    candidate = Path(state_path).parent / review_path
    if candidate.is_file():
        review = json.load(open(candidate))
        closure_attempts = review.get("closure_reviews") or []
        operations_attempts = review.get("operations_reviews") or []
        if closure_attempts or operations_attempts:
            print(
                f"Review attempts: closure={len(closure_attempts)}, "
                f"operations={len(operations_attempts)}"
            )

if state.get("operations_required"):
    if cur_phase in {"operationally-verified", "complete"}:
        probe = subprocess.run(
            [sys.executable, artifact_script, "gate", "--state", state_path,
             "--phase", "complete"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            print("Current operational evidence: FRESH")
        else:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            print("Current operational evidence: STALE/INVALID")
            if detail:
                print(f"  {detail[-1]}")
            print("  Enter verify-running and append a read-only refresh first; authorize apply only if drift is observed.")
    else:
        print("Current operational evidence: not yet verified")

    plan_file = Path(state_path).parent / str(state.get("operations_plan", ""))
    evidence_file = Path(state_path).parent / str(state.get("operations_evidence", ""))
    waiver_file = Path(state_path).parent / str(state.get("waivers", ""))
    if plan_file.is_file() and evidence_file.is_file():
        plan = json.load(open(plan_file))
        evidence = json.load(open(evidence_file))
        max_age = int(plan.get("max_evidence_age_seconds") or 0)
        active_waivers = {}
        if waiver_file.is_file():
            for waiver in json.load(open(waiver_file)).get("waivers", []):
                expiry = parse(waiver["expires_at"])
                if expiry > now:
                    active_waivers[waiver["target_id"]] = waiver["expires_at"]
        print("Operational targets:")
        for target in evidence.get("targets", []):
            latest = (target.get("attempts") or [None])[-1]
            latest_attempt_hash = None
            if latest is not None:
                latest_attempt_hash = hashlib.sha256(
                    json.dumps(latest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    .encode("utf-8")
                ).hexdigest()
            current_refreshes = [
                item for item in (target.get("verification_refreshes") or [])
                if item.get("source_attempt_sha256") == latest_attempt_hash
            ]
            observed = (
                current_refreshes[-1].get("observed_at")
                if current_refreshes
                else None if latest is None else latest.get("verification", {}).get("observed_at")
            )
            if observed:
                deadline = parse(observed) + timedelta(seconds=max_age)
                freshness = "fresh" if deadline >= now else "STALE"
                detail = f"observed={observed} deadline={deadline.strftime('%Y-%m-%dT%H:%M:%SZ')} {freshness}"
            else:
                detail = "no verification observation"
            if target.get("id") in active_waivers:
                detail += f" waiver-expires={active_waivers[target['id']]}"
            print(f"  {target.get('id')}: {target.get('status')} ({detail})")

NEXT = {
    "init":               "research-running (run Phase 1 of milestone-pipeline)",
    "research-running":   "research-complete (researchers in flight; await briefs)",
    "research-complete":  "implement-running (run Phase 2 of milestone-pipeline)",
    "implement-running":  "implement-complete (implementer in flight; await commit)",
    "implement-complete": "critique-running (run Phase 3 of milestone-pipeline)",
    "critique-running":   "critique-complete (await all independent critic receipts)",
    "critique-complete":  "rectify-running (run rectification)",
    "rectify-running":    "code-complete (requires implementation evidence + closure review)",
    "code-complete":      "publish-running, or complete only when publication+operations are explicitly not required",
    "publish-running":    "published (requires remote/render/artifact release evidence)",
    "published":          "plan-review-running, or complete when operations are explicitly not required",
    "plan-review-running": "plan-reviewed (latest append-only operations adversary attempt must PASS)",
    "plan-reviewed":      "apply-running (requires explicit target-scoped authorization)",
    "apply-running":      "applied (requires target-scoped authorization + apply attempts)",
    "applied":            "verify-running (or apply-running for another append-only attempt)",
    "verify-running":     "operationally-verified (or apply-running after failed verification)",
    "operationally-verified": "complete, or verify-running when evidence needs a read-only refresh",
    "complete":           "verify-running if live evidence becomes stale; apply only after observed drift",
}
print(f"Next phase:  {NEXT.get(cur_phase, '(unknown)')}")
PY
