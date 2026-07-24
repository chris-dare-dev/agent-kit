#!/usr/bin/env bash
#
# validate-spike-id.sh — shape check for a spike id (regex only).
#
# Usage:
#   validate-spike-id.sh <spike-id>
#
# Spike-id regex: ^[a-z][a-z0-9-]*-spike-[0-9]+$
#
# Examples (valid):
#   kiali-multicluster-spike-1
#   admin-mcp-spike-3
#   token-controller-spike-12
#
# Examples (invalid):
#   spike-1              (no leading topic)
#   Foo-spike-1          (uppercase)
#   foo-spike            (no number)
#   foo--spike-1         (double dash — would parse but rejected as a leak heuristic)
#
# Exit 0 valid; exit 2 invalid (with message on stderr).
#
# THIS IS A SHAPE CHECK ONLY. The roadmap entry (brief-existence) is validated
# by the spike-designer subagent in its Step 1 — not here.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: validate-spike-id.sh <spike-id>" >&2
  echo "" >&2
  echo "Validates the SHAPE of a spike id (regex check only)." >&2
  echo "Format: <topic>-spike-<N>  e.g. kiali-multicluster-spike-1" >&2
  echo "" >&2
  echo "Regex: ^[a-z][a-z0-9-]*-spike-[0-9]+\$" >&2
  echo "" >&2
  echo "Note: this script checks shape only. Brief-existence (roadmap entry)" >&2
  echo "is validated by the spike-designer subagent in Step 1." >&2
  exit 2
fi

ID="$1"

# Regex: must start with a lowercase letter, followed by zero or more lowercase
# letters/digits/hyphens, then -spike-, then one or more digits.
# Double-dash (--) is rejected as it indicates a malformed slug.
if [[ "$ID" =~ -- ]]; then
  echo "invalid spike-id '$ID': double-dash is not permitted in spike ids" >&2
  exit 2
fi

if [[ "$ID" =~ ^[a-z][a-z0-9-]*-spike-[0-9]+$ ]]; then
  # Valid
  exit 0
else
  echo "invalid spike-id '$ID'" >&2
  echo "Expected format: <topic>-spike-<N>" >&2
  echo "Regex: ^[a-z][a-z0-9-]*-spike-[0-9]+\$" >&2
  echo "" >&2
  echo "Valid examples:" >&2
  echo "  kiali-multicluster-spike-1" >&2
  echo "  admin-mcp-spike-3" >&2
  echo "  token-controller-spike-12" >&2
  echo "" >&2
  echo "Common mistakes:" >&2
  echo "  spike-1           — no leading topic segment" >&2
  echo "  Foo-spike-1       — uppercase letters not allowed" >&2
  echo "  foo-spike         — missing trailing number" >&2
  echo "  foo--spike-1      — double-dash not allowed" >&2
  exit 2
fi
