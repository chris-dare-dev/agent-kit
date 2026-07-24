#!/bin/bash
# PreToolUse hook (matcher: Bash) — gates direct kubectl mutations behind the
# harness permission prompt (permissionDecision: "ask").
#
# WHY: all cluster changes must go through GitOps (edit charts/ or infra/ source,
# CI renders into deploy/argocd-config-*, ArgoCD syncs). A direct kubectl mutation
# creates drift that ArgoCD silently reverts (selfHeal) or that masks the real
# state. This hook mechanically enforces the External System Write Policy for
# kubectl: it emits "ask", so the harness prompt IS the per-command confirmation
# gate — the engineer sees the exact command and approves or rejects it. This is
# deliberate: gated remediation runbooks (kargo-promotion.md reverify, /argoops
# fixes, /argocd-unstick, /eks-node-replace) say "present the command, get explicit
# confirmation, then execute" — an unconditional "deny" would bypass the harness
# prompt entirely and make those sanctioned gated fixes impossible to execute
# (2026-07-02 fix; the original version emitted "deny").
#
# GATED VERBS:   apply create delete patch edit scale annotate label cordon drain
#                taint replace rollout(restart|undo|pause|resume)
# ALLOWED:       all read-only kubectl (get, describe, logs, top, auth can-i,
#                rollout status, ...) and anything carrying --dry-run.
#
# MECHANICS: reads the PreToolUse JSON payload on stdin (same contract as
# data/hooks/validate-commit-subject.sh), extracts .tool_input.command via jq,
# strips shell-quoted content first (so `echo "kubectl delete"` does not trigger),
# then emits a structured "ask" decision. Fails OPEN on parse errors — a broken
# hook must not block unrelated work.
#
# Wire via .claude/settings.json:
#   PreToolUse / matcher "Bash" / command:
#     ${CLAUDE_PROJECT_DIR}/.claude/hooks/block-kubectl-mutations.sh
# (see data/scripts/template-settings.json + template-settings.md)

set -euo pipefail

input="$(cat)"
command="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"

# Fallback for older harnesses that exported TOOL_INPUT instead of stdin JSON.
if [ -z "$command" ]; then
  command="${TOOL_INPUT:-}"
fi
[ -z "$command" ] && exit 0

# Strip shell-quoted content so patterns inside echoed strings / jq filters /
# commit messages do not false-trigger (same approach as validate-commit-subject.sh).
stripped="$(printf '%s' "$command" | awk '
BEGIN { in_dq = 0; in_sq = 0 }
{
  n = length($0); i = 1
  while (i <= n) {
    c = substr($0, i, 1)
    if (in_dq) {
      if (c == "\\" && i < n) { i += 2; continue }
      if (c == "\"") in_dq = 0
    } else if (in_sq) {
      if (c == "'\''") in_sq = 0
    } else {
      if (c == "\"") in_dq = 1
      else if (c == "'\''") in_sq = 1
      else printf "%s", c
    }
    i++
  }
  printf "\n"
}
')"

# Mutation pattern: kubectl, optional flags (with or without separate values,
# e.g. `--context foo -n bar`), then a mutating verb as its own word.
# NOTE: [[:space:]] (POSIX class) not \s — BSD/macOS grep -E has no \s.
MUT='kubectl[[:space:]]+(-{1,2}[^[:space:]]+[[:space:]]+([^-[:space:]][^[:space:]]*[[:space:]]+)?)*(apply|create|delete|patch|edit|scale|annotate|label|cordon|drain|taint|replace|rollout[[:space:]]+(restart|undo|pause|resume))([[:space:]]|$)'

if ! printf '%s' "$stripped" | grep -qE "$MUT"; then
  exit 0
fi

# --dry-run anywhere in the real (unquoted) command text exempts it.
# grep -qe (not a bare pattern) — a pattern starting with -- is otherwise parsed
# as a grep OPTION and errors (this exact bug made the previous inline blocker
# silently inert).
if printf '%s' "$stripped" | grep -qe '--dry-run'; then
  exit 0
fi

jq -n --arg cmd "$command" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: ("GATED kubectl mutation (External System Write Policy). Cluster changes normally go through GitOps: edit the source (charts/, infra/, source/), let CI render into deploy/argocd-config-*, let ArgoCD sync. Approve this prompt ONLY if it is a sanctioned gated remediation you have reviewed (e.g. kargo reverify, /argoops fix, /argocd-unstick, /eks-node-replace). Use --dry-run for testing. Command: " + $cmd)
  }
}'
exit 0
