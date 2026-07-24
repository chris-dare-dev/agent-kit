#!/usr/bin/env bash
# Test harness for data/scripts/sealed-eval.py (self-improving-tooling-m5 E5 capstone).
# Run from the repo root: bash data/scripts/sealed-eval-test.sh
# Exits 0 iff all 8 cases pass.
# NOTE: cases run T1, T3, T2, T2b, T4, T5, T6, T7 — the clean-match (T3) runs before the
# destructive tamper cases (T2/T2b) on purpose, since those mutate temp copies; IDs reflect
# logical grouping, not execution order.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 2

HARNESS="data/scripts/sealed-eval.py"
CORPUS="data/scripts/sealed-eval-v0-corpus.jsonl"
MANIFEST="data/scripts/sealed-eval-v0-manifest.json"
TAMPER="$(mktemp -t sealed-eval-tampered.XXXXXX)"
trap 'rm -f "$TAMPER"' EXIT

FAILED=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILED=1; }

# T1 — seal-determinism: two seals over the same corpus give identical sha256.
SHA1="$(python3 "$HARNESS" seal --corpus "$CORPUS" --manifest "$TAMPER.m1" | sed -n 's/^seal: corpus_sha256=//p')"
SHA2="$(python3 "$HARNESS" seal --corpus "$CORPUS" --manifest "$TAMPER.m2" | sed -n 's/^seal: corpus_sha256=//p')"
rm -f "$TAMPER.m1" "$TAMPER.m2"
if [ -n "$SHA1" ] && [ "$SHA1" = "$SHA2" ]; then
  pass "T1 seal-determinism ($SHA1)"
else
  fail "T1 seal-determinism (sha1='$SHA1' sha2='$SHA2')"
fi

# T3 — clean-match: verify the committed corpus against its manifest.
OUT="$(python3 "$HARNESS" verify "$MANIFEST" --corpus "$CORPUS")"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "MATCH"; then
  pass "T3 clean-match"
else
  fail "T3 clean-match (rc=$RC out='$OUT')"
fi

# T2 — tamper-mismatch (load-bearing: covers the SP3 verify-path bug).
cp "$CORPUS" "$TAMPER"
python3 - "$TAMPER" <<'PY'
import sys
p = sys.argv[1]
data = bytearray(open(p, "rb").read())
needle = b'"critique_critical": 2'
i = data.index(needle)
data[i + len(b'"critique_critical": ')] = ord("9")
open(p, "wb").write(data)
PY
OUT="$(python3 "$HARNESS" verify "$MANIFEST" --corpus "$TAMPER")"; RC=$?
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q "MISMATCH"; then
  pass "T2 tamper-mismatch (rc=$RC)"
else
  fail "T2 tamper-mismatch (rc=$RC out='$OUT') — verify-path bug NOT fixed"
fi

# T2b — verify --corpus is load-bearing (explicit SP3 dest-collision regression guard):
#   seal a CLEAN copy A with its OWN manifest (corpus_path=A), then verify that manifest against
#   a TAMPERED copy B via --corpus. If --corpus were ignored (fallback to manifest.corpus_path=A,
#   which is clean) it would report MATCH/exit 0 — the exact SP3 false-MATCH. MISMATCH proves
#   the --corpus override wins, pinning the regression directly.
CLEAN_A="$(mktemp -t sealed-eval-cleanA.XXXXXX)"
MANIFEST_A="$(mktemp -t sealed-eval-manA.XXXXXX)"
TAMPER_B="$(mktemp -t sealed-eval-tamperB.XXXXXX)"
cp "$CORPUS" "$CLEAN_A"
python3 "$HARNESS" seal --corpus "$CLEAN_A" --manifest "$MANIFEST_A" >/dev/null
cp "$CORPUS" "$TAMPER_B"
python3 - "$TAMPER_B" <<'PY'
import sys
p = sys.argv[1]
data = bytearray(open(p, "rb").read())
i = data.index(b'"critique_critical": 2')
data[i + len(b'"critique_critical": ')] = ord("9")
open(p, "wb").write(data)
PY
OUT="$(python3 "$HARNESS" verify "$MANIFEST_A" --corpus "$TAMPER_B")"; RC=$?
rm -f "$CLEAN_A" "$MANIFEST_A" "$TAMPER_B"
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q "MISMATCH"; then
  pass "T2b verify-corpus-is-load-bearing"
else
  fail "T2b verify-corpus-is-load-bearing (rc=$RC out='$OUT') — --corpus ignored (dest-collision regression)"
fi

# T4 — metric-n-and-guard.
OUT="$(python3 "$HARNESS" metric --corpus "$CORPUS")"
if printf '%s' "$OUT" | grep -q "n_baseline=3" \
  && printf '%s' "$OUT" | grep -q "n_post=1" \
  && printf '%s' "$OUT" | grep -q "overclaim_guard_fired=True" \
  && printf '%s' "$OUT" | grep -q "insufficient n"; then
  pass "T4 metric-n-and-guard"
else
  fail "T4 metric-n-and-guard (out='$OUT')"
fi

# T5 — min-n-guard-fires: no fabricated win language when the guard is up.
# The honest verdict legitimately says "no reduction claimed"; strip that phrase
# first, then any residual "improved"/"reduction" would be a fabricated win.
RESIDUAL="$(printf '%s' "$OUT" | sed 's/no reduction claimed//g')"
if printf '%s' "$RESIDUAL" | grep -Eqi "improved|reduction"; then
  fail "T5 min-n-guard-fires (found win language: '$OUT')"
else
  pass "T5 min-n-guard-fires"
fi

# T6 — bad-name-rejected.
OUT="$(python3 "$HARNESS" probe-bad-name)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "REJECTED" && printf '%s' "$OUT" | grep -q "METRIC_GUARD"; then
  pass "T6 bad-name-rejected"
else
  fail "T6 bad-name-rejected (rc=$RC out='$OUT')"
fi

# T7 — baseline pins to the committed v0 seal, not the mutable live corpus (M1 guard):
#   append a synthetic high-(C+H) milestone to a COPY of the v0 corpus, run BARE metric on the
#   copy. The baseline must stay 1.0 (derived from the committed v0 seal), not drift to absorb
#   the synthetic record — bare metric defaults --baseline to the committed v0 manifest.
DRIFT="$(mktemp -t sealed-eval-drift.XXXXXX)"
cp "$CORPUS" "$DRIFT"
printf '%s\n' '{"schema_version":1,"pipeline":"milestone","id":"self-improving-tooling-m99-synthetic","status":"complete","critique_critical":9,"critique_high":9,"critique_medium":0,"critique_low":0,"rectification_count":1,"token_cost":1}' >> "$DRIFT"
OUT="$(python3 "$HARNESS" metric --corpus "$DRIFT")"
rm -f "$DRIFT"
if printf '%s' "$OUT" | grep -q "baseline_ch_per_milestone=1.00"; then
  pass "T7 baseline-pinned-to-v0-seal"
else
  fail "T7 baseline-pinned-to-v0-seal (out='$OUT')"
fi

if [ "$FAILED" -eq 0 ]; then
  printf '\nALL TESTS PASSED\n'
  exit 0
fi
printf '\nTESTS FAILED\n'
exit 1
