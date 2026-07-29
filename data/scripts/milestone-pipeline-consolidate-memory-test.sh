#!/usr/bin/env bash
#
# milestone-pipeline-consolidate-memory-test.sh — tests for the E3/C3 --extract-trajectories
# subcommand of milestone-pipeline-consolidate-memory.sh (self-improving-tooling-m3).
#
# Mirrors the repo's per-script test convention (pipeline-outcome-log-test.py,
# agent-memory-consolidate-test.py). Pure bash + stdlib python3, no deps, hermetic
# (runs entirely under a mktemp -d sandbox; never touches the real agent-memory).
#
# Asserts:
#   T1  the 5 expected rules fire on the frozen-baseline fixture (RULE-1..5) in --dry-run
#   T2  a real run appends exactly those 5 lessons, routed to the right agent dirs
#   T3  idempotency — a second real run appends 0 (all SKIP exists)
#   T4  every emitted lesson line parses under dedupe_one_file()'s LINE_RE
#   T5  empty/absent corpus is a clean no-op (exit 0)
#
# Usage:  bash data/scripts/milestone-pipeline-consolidate-memory-test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SCRIPT_DIR/milestone-pipeline-consolidate-memory.sh"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

FIXTURE="$SANDBOX/outcomes.jsonl"
export AGENT_MEMORY_ROOT="$SANDBOX/agent-memory"   # isolate writes from the real corpus

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
assert_contains() { case "$2" in *"$1"*) ok "$3" ;; *) bad "$3 (missing: $1)" ;; esac; }

# --- Frozen-baseline fixture: 3 milestone + 4 spike + 1 roadmap, all token_cost null. ---
cat > "$FIXTURE" <<'JSONL'
{"schema_version":1,"pipeline":"milestone","id":"self-improving-tooling-m4","status":"complete","rectification_count":1,"critique_critical":2,"critique_high":1,"critique_medium":5,"critique_low":4,"token_cost":null}
{"schema_version":1,"pipeline":"milestone","id":"self-improving-tooling-m4b","status":"complete","rectification_count":1,"critique_critical":0,"critique_high":0,"critique_medium":3,"critique_low":2,"token_cost":null}
{"schema_version":1,"pipeline":"milestone","id":"self-improving-tooling-m4b-pivot","status":"complete","rectification_count":1,"critique_critical":0,"critique_high":0,"critique_medium":2,"critique_low":3,"token_cost":null}
{"schema_version":1,"pipeline":"spike","id":"self-improving-tooling-otel-spike-1","status":"accept","verdict":"NO","token_cost":null}
{"schema_version":1,"pipeline":"spike","id":"self-improving-tooling-otel-spike-2","status":"accept","verdict":"YES","token_cost":null}
{"schema_version":1,"pipeline":"spike","id":"coherence-gate-producers-spike-2","status":"accept","verdict":"YES","token_cost":null}
{"schema_version":1,"pipeline":"spike","id":"dispatcher-verification-probes-spike-1","status":"accept","verdict":"","token_cost":null}
{"schema_version":1,"pipeline":"roadmap","id":"kargo-promotion-pipeline","status":"complete","token_cost":null}
JSONL

echo "T1: --dry-run fires the 5 expected rules"
DRY="$(bash "$TARGET" --extract-trajectories --log "$FIXTURE" --dry-run 2>&1)"
assert_contains "[n=3] milestone corpus is sparse"            "$DRY" "RULE-5 sparsity advisory"
assert_contains "C+H findings are heavily concentrated"       "$DRY" "RULE-1 C+H concentration"
assert_contains "self-improving-tooling-m4, C+H=3 of 3"       "$DRY" "RULE-1 names m4 with C+H=3 of 3"
assert_contains "rect_count was uniform (1)"                  "$DRY" "RULE-2 uniform rect"
assert_contains "range 0-3"                                   "$DRY" "RULE-2 reports C+H range 0-3"
assert_contains "1 of 4 spikes returned NO"                   "$DRY" "RULE-3 spike NO verdict"
assert_contains "token_cost is null in all 8 records"         "$DRY" "RULE-4 token null advisory"
case "$DRY" in *"dry-run: 5 candidate lessons"*) ok "exactly 5 candidate lessons" ;;
  *) bad "expected 5 candidate lessons; got: $(printf '%s' "$DRY" | grep -o 'dry-run: [0-9]* candidate')" ;; esac
[ -d "$AGENT_MEMORY_ROOT" ] && bad "dry-run wrote to disk" || ok "dry-run wrote nothing"

echo "T2: real run appends 5 lessons, routed correctly"
RUN1="$(bash "$TARGET" --extract-trajectories --log "$FIXTURE" 2>&1)"
assert_contains "appended 5, skipped 0" "$RUN1" "first real run appends 5"
adv=$(grep -c 'trajectory:' "$AGENT_MEMORY_ROOT/milestone-adversary/lessons.md" 2>/dev/null || echo 0)
rec=$(grep -c 'trajectory:' "$AGENT_MEMORY_ROOT/milestone-rectifier/lessons.md" 2>/dev/null || echo 0)
trj=$(grep -c 'trajectory:' "$AGENT_MEMORY_ROOT/trajectory/lessons.md" 2>/dev/null || echo 0)
[ "$adv" = 1 ] && ok "milestone-adversary got 1 (RULE-1)" || bad "milestone-adversary got $adv, want 1"
[ "$rec" = 1 ] && ok "milestone-rectifier got 1 (RULE-2)" || bad "milestone-rectifier got $rec, want 1"
[ "$trj" = 3 ] && ok "trajectory got 3 (RULE-3/4/5)"      || bad "trajectory got $trj, want 3"

echo "T3: idempotency — second real run appends 0"
RUN2="$(bash "$TARGET" --extract-trajectories --log "$FIXTURE" 2>&1)"
assert_contains "appended 0, skipped 5" "$RUN2" "second run is a no-op"

echo "T4: every emitted line parses under dedupe LINE_RE"
python3 - "$AGENT_MEMORY_ROOT" <<'PY'
import re, sys, pathlib
# Same LINE_RE as dedupe_one_file() in the target script.
LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+\[(?P<id>[^\]]+)\]\s+"
    r"(?:(?P<mode>[A-Za-z0-9_\-]+):\s+)?(?P<text>.+)$")
base = pathlib.Path(sys.argv[1])
bad = 0
for f in base.rglob("lessons.md"):
    for ln in f.read_text().splitlines():
        if not ln.strip():
            continue
        m = LINE_RE.match(ln)
        if not m or m.group("mode") != "trajectory":
            print("  FAIL unparsable/mismode: %s" % ln); bad += 1
if bad:
    sys.exit(1)
print("  ok   all emitted lines parse with mode=trajectory")
PY

echo "T5: absent corpus is a clean no-op"
NOOP="$(bash "$TARGET" --extract-trajectories --log "$SANDBOX/does-not-exist.jsonl" 2>&1)"
assert_contains "nothing to extract" "$NOOP" "absent corpus prints no-op + exits 0"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
