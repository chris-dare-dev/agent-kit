#!/usr/bin/env bash
# tests/check-skill-prompts.sh — guard required substrings in canonical
# milestone-pipeline prompts. Failing this script means a future docs edit
# silently dropped a rule that was added to close a known failure mode.
#
# Run manually:
#   bash tests/check-skill-prompts.sh
#
# Exit 0 if all assertions pass; non-zero (1) with the first failed assertion
# printed to stderr.
#
# Adding a new assertion:
#   - Cite the failure mode that motivated the substring (commit SHA + 1-line note).
#   - Choose the smallest distinctive substring that survives reasonable
#     wording reflows. Avoid full sentences; single-token / short-phrase greps
#     are durable across rewordings, full-sentence greps are not.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Canonical homes since the milestone-pipeline skill was retired (2026-07 overhaul):
# the per-agent prompts live in data/agents/milestone-{implementer,rectifier}.md and
# the validation table in data/references/milestone-pipeline-phase-implement.md.
IMPLEMENTER="$REPO_ROOT/data/agents/milestone-implementer.md"
RECTIFIER="$REPO_ROOT/data/agents/milestone-rectifier.md"
TABLE="$REPO_ROOT/data/references/milestone-pipeline-phase-implement.md"
INFRA_REVIEWER="$REPO_ROOT/data/agents/milestone-infra-safety.md"
CODEX_MILESTONE_ADAPTER="$REPO_ROOT/data/provider-adapters/codex/entrypoints/milestone-pipeline/SKILL.md"
HANDOFF_SKILL="$REPO_ROOT/data/skills/handoff/SKILL.md"
ROADMAP_COMMAND="$REPO_ROOT/data/commands/roadmap.md"
SPIKE_COMMAND="$REPO_ROOT/data/commands/spike.md"
MILESTONE_COMMAND="$REPO_ROOT/data/commands/milestone-pipeline.md"

if [[ ! -f "$IMPLEMENTER" ]]; then
  echo "FAIL: implementer agent file not found at $IMPLEMENTER" >&2
  exit 1
fi
if [[ ! -f "$RECTIFIER" ]]; then
  echo "FAIL: rectifier agent file not found at $RECTIFIER" >&2
  exit 1
fi
if [[ ! -f "$TABLE" ]]; then
  echo "FAIL: validation-table file not found at $TABLE" >&2
  exit 1
fi
if [[ ! -f "$INFRA_REVIEWER" ]]; then
  echo "FAIL: infra-safety reviewer file not found at $INFRA_REVIEWER" >&2
  exit 1
fi
if [[ ! -f "$CODEX_MILESTONE_ADAPTER" ]]; then
  echo "FAIL: Codex milestone adapter not found at $CODEX_MILESTONE_ADAPTER" >&2
  exit 1
fi

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

# admin-mcp-phase-2 retro: implementer + rectifier sub-agents committed code
# that passed `ruff check` / `bun run lint` but was rejected by CI's
# `ruff format --check` / `prettier --check`. Closing that gap requires the
# formatter rule to remain in the canonical prompts; this script gates that.

# H1 + L1 — Implementer agent body must mention a formatter command
grep -q -E "ruff format|prettier --write|format:check" "$IMPLEMENTER" \
  || fail "Implementer agent no longer mentions a formatter command (ruff format / prettier --write / format:check)"

# H1 — Implementer must scope formatter to touched files, not run repo-wide
# (matches '<touched' or 'touched files' or 'touched-' to allow wording reflow)
grep -q -E "touched.{0,10}files|<touched" "$IMPLEMENTER" \
  || fail "Implementer agent must scope formatter to touched files (no repo-wide ruff format . / bun run format)"

# M1 — Helm-chart YAML guidance: must use yamllint (which catches duplicate
# keys), NOT yaml.safe_load alone (which silently keeps the last value).
# If yaml.safe_load is referenced at all, yamllint must also be present.
if grep -q "yaml.safe_load" "$IMPLEMENTER"; then
  grep -q "yamllint" "$IMPLEMENTER" \
    || fail "Implementer Helm-chart guidance references yaml.safe_load alone; yamllint is required (yaml.safe_load silently accepts duplicate keys)"
fi

# M4 — Rectifier agent must also reference a formatter command. Phase 4
# rectification commits hit the same CI gate as Phase 2 implementer commits.
grep -q -E "ruff format|prettier|format:check|formatter" "$RECTIFIER" \
  || fail "Rectifier agent does not reference a formatter command (will silently re-introduce admin-mcp-phase-2-style CI failures)"

# Validation table must list ruff format --check (Python) and format:check (JS).
grep -q "ruff format --check" "$TABLE" \
  || fail "Validation table does not gate 'ruff format --check' (admin-mcp-phase-2 lesson)"
grep -q "format:check" "$TABLE" \
  || fail "Validation table does not gate '*:format:check' for JS (admin-mcp-phase-2 lesson)"

# M3 — Validation table preamble must hint at lockfile-based row selection,
# so a sub-agent dispatched to an unfamiliar repo can pick the correct row.
grep -q -E "lockfile|uv\.lock|bun\.lockb" "$TABLE" \
  || fail "Validation table is missing a lockfile / project-marker hint for row selection"

# SVCR-HOSTNAME-ISTIO-AUTOSYNC-20260714: high-risk repository identity is an
# independent OR-selector. Once selected by identity, the reviewer must audit
# the entire diff and cannot emit the former malformed one-line early exit.
grep -q "deterministic selector uses an OR" "$INFRA_REVIEWER" \
  || fail "infra-safety reviewer lost the identity OR path selector contract"
grep -q "entire implementation diff" "$INFRA_REVIEWER" \
  || fail "identity-selected infra review no longer covers the entire diff"
if grep -q "diff contains no infra-safety-scope files; nothing to critique" "$INFRA_REVIEWER"; then
  fail "infra-safety reviewer restored the forbidden malformed early exit"
fi

# The Codex entrypoint must expose the exact preauthorized observer branch and
# keep generic controller authorization fail-closed.
grep -q "attempt-adopt-auto-sync" "$CODEX_MILESTONE_ADAPTER" \
  || fail "Codex milestone adapter lost the non-mutating auto-sync observer"
grep -q "generic auto-sync is forbidden" "$CODEX_MILESTONE_ADAPTER" \
  || fail "Codex milestone adapter no longer forbids generic auto-sync"

# Phase 5 artifact integration: successful skill terminals emit immutable
# routing receipts, never direct sink writes. Keep every canonical producer and
# the hand-authored Codex milestone adapter on the shared capture contract.
for contract in \
  "$HANDOFF_SKILL:--producer handoff" \
  "$ROADMAP_COMMAND:--producer roadmap" \
  "$SPIKE_COMMAND:--producer spike" \
  "$MILESTONE_COMMAND:--producer milestone-pipeline" \
  "$CODEX_MILESTONE_ADAPTER:--producer milestone-pipeline"
do
  file="${contract%%:*}"
  producer="${contract#*:}"
  grep -q "artifact_skill_capture.py" "$file" \
    || fail "$(basename "$file") lost the Phase 5 artifact capture hook"
  grep -q -- "$producer" "$file" \
    || fail "$(basename "$file") lost its Phase 5 producer identity"
  grep -qi "bulk ingestion" "$file" \
    || fail "$(basename "$file") no longer names the Graphiti bulk-ingestion lock"
  grep -qi "disabled" "$file" \
    || fail "$(basename "$file") no longer pins the ingestion lock disabled"
done

grep -q "successful terminals only" "$SPIKE_COMMAND" \
  || fail "Spike capture no longer excludes failed/retry terminals"
grep -q "After .*complete.*succeeds" "$MILESTONE_COMMAND" \
  || fail "Milestone capture is no longer gated after authoritative completion"

echo "OK — all skill-prompt assertions pass."
