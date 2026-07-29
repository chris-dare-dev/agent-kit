#!/bin/bash
# PreToolUse hook (matcher: Edit|Write) — blocks hand-edits to CI-rendered
# deploy-repo content.
#
# WHY: everything under deploy/argocd-config-* is written by the CI pipeline
# (and, for ArgoCD's own root-app-of-apps, by Pulumi) — a hand-edit is silently
# overwritten on the next render and diverges the clone from origin.
#
# NARROW EXCEPTIONS (owner-approved) — both are HAND-MAINTAINED, not CI-rendered.
# Neither is a licence to push: both still require External-Write-Policy
# confirmation, and the deploy repo demands `git pull --ff-only` first (CI writes
# land constantly).
#
#   1. ApplicationSets — deploy/argocd-config-*/applicationsets/{env}/*.yaml
#      (post-incident rule — see platform/CLAUDE.md "GitOps Guardrail").
#
#   2. Repo-ROOT meta — deploy/argocd-config-*/{README,CLAUDE,CHANGELOG,
#      CONTRIBUTING}.md (owner-approved 2026-07-16). The deploy repo's own
#      CLAUDE.md § "The three content types" lists these as "safe to maintain
#      (gated)", and git agrees: GitLab CI has never authored one (0 commits;
#      their entire history is human `docs:` commits). Blocking them was strictly
#      over-broad — and it had a real cost: it blocked the fix for a stale branch
#      table that claimed the prod branch is `main` when the branches are
#      dev/stage/prod/test, which had already sent a session chasing a phantom
#      "render never landed" (`git show origin/main:…` → Not a valid object name).
#      A doc that is confidently wrong about prod is a hazard the hook should not
#      be protecting. ROOT ONLY — the regex anchors one path segment after the
#      repo name, so a meta-named file nested under apps/ stays blocked.
#
# Patterns blocked (BOTH layouts, old account + current):
#   gitops-config/apps/.*manifest.yaml        (legacy old-account repo name)
#   deploy/argocd-config-<anything>/...       (current deploy repos)
# Carve-outs: deploy/argocd-config-<anything>/applicationsets/...
#             deploy/argocd-config-<anything>/{README,CLAUDE,CHANGELOG,CONTRIBUTING}.md
#
# MECHANICS: reads the PreToolUse JSON payload on stdin, extracts
# .tool_input.file_path via jq, emits a structured deny decision. Fails OPEN on
# parse errors. Wire via .claude/settings.json PreToolUse matcher "Edit|Write"
# (see data/scripts/template-settings.json + template-settings.md).
#
# LIMITATION: matches on the path STRING. If a session's cwd is INSIDE the
# deploy repo and the tool gets a relative path with no `deploy/argocd-config-`
# segment, this hook cannot see it — do not start sessions inside deploy repos.

set -euo pipefail

input="$(cat)"
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"

if [ -z "$fp" ]; then
  fp="${TOOL_INPUT:-}"
fi
[ -z "$fp" ] && exit 0

# Carve-outs first: hand-maintained content is allowed.
#   applicationsets/ — the post-incident AppSet rule.
#   repo-ROOT meta   — `[^/]+/` matches the repo dir, then the filename must be
#                      the LAST segment ($), so apps/**/CLAUDE.md stays blocked.
if printf '%s' "$fp" | grep -qE 'deploy/argocd-config-[^/]+/applicationsets/'; then
  exit 0
fi
if printf '%s' "$fp" | grep -qE 'deploy/argocd-config-[^/]+/(README|CLAUDE|CHANGELOG|CONTRIBUTING)\.md$'; then
  exit 0
fi

if printf '%s' "$fp" | grep -qE 'gitops-config/apps/.*manifest\.yaml|deploy/argocd-config-[^/]+/'; then
  jq -n --arg fp "$fp" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("BLOCKED: " + $fp + " is CI-rendered deploy-repo content — hand-edits are overwritten on the next render and diverge the clone from origin. Change the SOURCE instead (charts/ Helm values, source/ Dockerfiles, infra/ IaC) and let the pipeline propagate. Exception: applicationsets/{env}/*.yaml are hand-maintained (gated) and are not blocked.")
    }
  }'
fi
exit 0
