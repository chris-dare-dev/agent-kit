#!/bin/bash
# PreToolUse hook — validates `git commit` invocations before they run.
#
# Two checks, in order:
#   1. GPG signing: -S or --gpg-sign must be present (platform repos require
#      signed commits server-side). --no-gpg-sign is forbidden.
#   2. Conventional-commit subject: must match the platform regex
#      ^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}$
#
# Reads the tool invocation JSON on stdin and either allows (empty output +
# exit 0) or blocks with a structured decision JSON on stdout.
#
# Only validates `git commit` with an explicit `-m "..."` (or -m '...'). If no
# -m is provided, the hook allows — editor mode defers to git's own commit-msg
# hooks and config-driven signing.

set -euo pipefail

# Read the tool invocation JSON from stdin.
input="$(cat)"

# Extract the shell command string. Claude Code passes Bash invocations as
# { "tool_input": { "command": "..." } }.
command="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

# If we can't parse the command, fail open (allow) — don't block on hook bugs.
if [ -z "$command" ]; then
  exit 0
fi

# Strip shell-quoted content before any substring matching, so the hook only
# fires on real command invocations — not on patterns that happen to appear
# inside echoed strings, jq filters, test harnesses, or JSON payloads.
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

# Only validate `git commit` invocations (matched in stripped, so patterns
# inside quoted strings do not trigger).
case "$stripped" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

# Extract the subject. Try double-quoted `-m "..."` first, then single-quoted.
# We grab only the first occurrence (the subject); subsequent -m flags are the
# body and are not subject to the 50-char rule.
subject=""
if [[ "$command" =~ -m[[:space:]]+\"([^\"]+)\" ]]; then
  subject="${BASH_REMATCH[1]}"
elif [[ "$command" =~ -m[[:space:]]+\'([^\']+)\' ]]; then
  subject="${BASH_REMATCH[1]}"
fi

# If there's no -m (editor mode) or we couldn't extract, allow — the editor +
# git's own commit-msg hook will enforce subject format, and git config /
# commit.gpgsign will handle signing.
if [ -z "$subject" ]; then
  exit 0
fi

emit_deny() {
  local reason="$1"
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

# --- Check 1: GPG signing ---
# Platform repos enforce signed commits server-side. Require -S or --gpg-sign
# on any non-interactive commit. Matches short-flag bundles too (e.g. -Ss,
# -SkeyID, -sS). Also accepts `-c commit.gpgsign=true` as an explicit opt-in.
#
# Explicit --no-gpg-sign is never allowed from here — CLAUDE.md forbids it.
# All checks run against $stripped so patterns inside quoted strings do not
# false-trigger.
if [[ "$stripped" =~ --no-gpg-sign ]]; then
  emit_deny "Commit uses --no-gpg-sign, which is forbidden by ~/Work/workspace/CLAUDE.md (Git Safety Protocol). Platform repos require signed commits; remove the flag and use -S instead."
fi

# Match -S anywhere as its own flag or bundled with other short flags.
# Valid: -S, -Ss, -sS, -SABCDEF, "git commit -S -m ..."
if ! [[ "$stripped" =~ (^|[[:space:]])-[a-zA-Z]*S([a-zA-Z0-9]*)?([[:space:]]|$) ]] \
   && ! [[ "$stripped" =~ --gpg-sign($|[[:space:]=]) ]] \
   && ! [[ "$stripped" =~ -c[[:space:]]+commit\.gpgsign=true ]]; then
  emit_deny "$(cat <<EOF
Commit is missing -S (GPG signing). Platform repos enforce signed commits on
the server side — an unsigned commit will be rejected on push.

Your command: $command

Add -S to the commit:
  git commit -S -m "$subject"

Or set git config globally so signing is automatic:
  git config --global commit.gpgsign true
  git config --global user.signingkey <your-key-id>

See ~/Work/workspace/CLAUDE.md "Git Conventions" → GPG Commit Signing.
EOF
)"
fi

# --- Check 2: conventional-commit subject ---
regex='^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}$'
if [[ "$subject" =~ $regex ]]; then
  exit 0
fi

emit_deny "$(cat <<EOF
Commit subject does not match the platform's conventional-commit regex.

Your subject: "$subject"

Required format:
  ^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\(.+\))?: .{1,50}$

Examples:
  fix: correct tenant-example SE port
  docs: add chart CLAUDE.md
  feat(cli): add platform check drift subcommand

Rules:
- One of the listed types is required, followed by ": "
- Optional parenthesized scope allowed
- Description after the colon must be 1 to 50 characters
- GitLab's server-side push rule enforces the same regex — a non-compliant
  commit will be rejected on push. This hook blocks locally to save you the
  round-trip.

See ~/Work/workspace/CLAUDE.md "Git Conventions" section.
EOF
)"
