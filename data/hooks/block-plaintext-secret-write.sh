#!/bin/bash
# PreToolUse hook (matchers: Edit|Write AND Bash) — blocks reintroducing a
# PLAINTEXT credential into the local agent-config files that carry secrets:
#   .mcp.json · .codex/config.toml · settings.local.json · .claude.json
#   · .claude/settings.json · claude_desktop_config.json  (the last two are the
#     2026-07-12 incident's own registration files — rectification M1)
#
# PROVIDER-AGNOSTIC CORE (SP1 decision, provider-neutral-agent-kit-remediation):
# one script body serves three thin adapters —
#   Claude Code  .claude/settings.json   PreToolUse Edit|Write + Bash matchers
#   Codex        workspace/.codex/hooks.json same .tool_input.* payload contract
#   OpenCode     platform/.opencode/plugin/secret-guard.js maps metadata.* into
#                the same {tool_input:{file_path,content}|{command}} shape and
#                pipes it to THIS script via BunShell.
#
# WHY: those files historically held a plaintext CONFLUENCE_API_TOKEN. Secrets
# there must use INDIRECTION instead — `${VAR}` env expansion in .mcp.json
# (officially supported), or shell-env inherit (`inherit = "core"`) for
# .codex/config.toml, or the ~/.config/workspace/secrets.env wrapper (README
# "Rotating an exposed credential") — so the literal secret never lands in a
# file. This hook is the systemic guard that keeps them clean.
#
# MECHANICS: reads the PreToolUse JSON on stdin.
#   Edit|Write branch: extracts .tool_input.file_path and the proposed content
#     (.tool_input.content for Write, .tool_input.new_string for Edit).
#   Bash branch (S1.2 — REDUCES the historical Bash-tool bypass; coverage set
#     below is the honest boundary): extracts .tool_input.command; a command is
#     only a candidate when its QUOTE-STRIPPED form contains write syntax
#     targeting a guarded file — quote-stripping (single-word quoted tokens are
#     kept, so quoted paths still count as targets) prevents false positives on
#     commit messages / echoed prose that merely MENTION a redirect. The
#     secret-shape scan then runs on the RAW command so a quoted secret is
#     still caught.
#
# BASH-BRANCH COVERAGE (kept honest — V-H1 rectification):
#   COVERED  write verbs: > >> >| &> · tee · sed -i · cp/mv · dd of= · curl -o
#   COVERED  wrappers: a leading/compound `bash -c` / `sh -c` / `zsh -c` /
#            `eval` (optionally `env`-prefixed) has its quoted payload peeled
#            and re-fed through the SAME detector (depth-capped) — otherwise
#            the quote-strip would drop the whole payload.
#   COVERED  staged sources: `cp/mv <staged> <guarded>` content-scans the
#            SOURCE file head (256 KiB, fail-open when unreadable) — the
#            default agent reflex after a denied direct write.
#   NOT covered (documented punts, codified as ALLOW fixtures in the test):
#            interpreter-level writes (`python3 -c 'open(...).write(...)'`,
#            `node -e 'fs.writeFileSync(...)'`, awk print-redirect inside a
#            quoted program). Defense-in-depth for those: the Edit|Write guard
#            path (agents editing configs normally use Edit/Write) plus the
#            repo secret scan (CI Gate 1n + pre-push) for tracked files.
# Detection is a SELF-CONTAINED heuristic (no external scanner) — deterministic
# and portable: gitleaks is neither guaranteed installed on every engineer's
# machine nor (as of 8.30.1) reliable at flagging the Atlassian-token shape
# here. A `${VAR}` indirection value is explicitly allowed. Fails OPEN on parse
# errors (a broken hook must never wedge all edits). Emits the same structured
# deny shape as the sibling PreToolUse hooks.
#
# PATTERN-DRIFT NOTE: data/scripts/secret-scan.py ports these two heuristics to
# Python for the CI/pre-push repo scan (Gate 1n). Edit a pattern HERE -> edit it
# THERE too, or the CI scan and the live guard silently diverge.

set -euo pipefail

input="$(cat)"

# Path separator class accepts / AND \ — Windows harnesses deliver
# C:\...\.mcp.json and the original (^|/) anchor silently never matched
# (found live on the Windows port, 2026-07-22).
GUARDED='(^|[/\\])\.mcp\.json$|(^|[/\\])\.codex[/\\]config\.toml$|(^|[/\\])settings\.local\.json$|(^|[/\\])\.claude\.json$|(^|[/\\])\.claude[/\\]settings\.json$|claude_desktop_config\.json$'
GUARDED_BASH='\.mcp\.json|\.codex/config\.toml|settings\.local\.json|\.claude\.json|\.claude/settings\.json|claude_desktop_config\.json'

# Two-pass secret detector shared by both branches. $1 = text to scan.
# Echoes a reason string, or empty. Pass 2 allows `${VAR}` indirection (first
# value char may not be `$` or `{`) — the mechanism S1.1 mandates stays legal.
leak_in() {
  if printf '%s' "$1" | grep -qE 'ATATT3xFfGF0|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-(ant|proj)-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'; then
    echo "a known credential token shape"; return
  fi
  if printf '%s' "$1" | grep -qiE '(token|secret|password|api[_-]?key|access[_-]?key)"?[[:space:]]*[:=][[:space:]]*"?[^"$[:space:]{][^"[:space:]]{11,}'; then
    echo "a plaintext value assigned to a secret-named key"; return
  fi
  echo ""
}

deny() { # $1 = target label, $2 = reason
  jq -n --arg fp "$1" --arg why "$2" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("BLOCKED: " + $fp + " appears to contain " + $why + ". Do not put plaintext credentials in agent-config files — use indirection: ${CONFLUENCE_API_TOKEN}-style env expansion in .mcp.json, or shell-env inherit (inherit=\"core\") for .codex/config.toml. Keep the secret in the shell env / Keychain.")
    }
  }'
}

# --- Branch 1: Edit|Write (behaviour-identical to the pre-S1.2 shipped guard) --
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
if [ -n "$fp" ]; then
  # Only guard the known local secret-bearing config files.
  if ! printf '%s' "$fp" | grep -qE "$GUARDED"; then
    exit 0
  fi
  # Proposed content: Write -> .content, Edit -> .new_string.
  content="$(printf '%s' "$input" | jq -r '.tool_input.content // .tool_input.new_string // empty' 2>/dev/null || true)"
  [ -z "$content" ] && exit 0
  leak="$(leak_in "$content")"
  [ -z "$leak" ] && exit 0
  deny "$fp" "$leak"
  exit 0
fi

# --- Branch 2: Bash command path (S1.2 — the promoted SP1 POC branch) ----------
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0

# Strip shell-quoted content BUT keep single-word quoted tokens (quoted paths
# like ".mcp.json" stay visible as write targets; quoted prose — anything with
# whitespace, e.g. a commit message that mentions `echo x > .mcp.json` — is
# dropped so it can never false-trigger the write-syntax match). Same awk
# state-machine approach as .codex/hooks/block-kubectl-mutations.sh.
strip_quotes() {
  printf '%s' "$1" | awk '
BEGIN { in_dq = 0; in_sq = 0; buf = "" }
{
  n = length($0); i = 1
  while (i <= n) {
    c = substr($0, i, 1)
    if (in_dq) {
      if (c == "\\" && i < n) { buf = buf substr($0, i, 2); i += 2; continue }
      if (c == "\"") { in_dq = 0; if (buf !~ /[[:space:]]/) printf "%s", buf; buf = "" }
      else buf = buf c
    } else if (in_sq) {
      if (c == "'\''") { in_sq = 0; if (buf !~ /[[:space:]]/) printf "%s", buf; buf = "" }
      else buf = buf c
    } else {
      if (c == "\"") { in_dq = 1 }
      else if (c == "'\''") { in_sq = 1 }
      else printf "%s", c
    }
    i++
  }
  printf "\n"
}
'
}

# Write-verb candidate set over the QUOTE-STRIPPED text (V-H1/M4 widened):
# > / >> / >| (clobber; &> is caught by the bare-> alternative) · tee ·
# sed -i · cp/mv · dd of= · curl -o (…and --output via the same -o stem).
WRITE_RE='(>{1,2}\|?[[:space:]]*[^|;&]*('"$GUARDED_BASH"')|tee[[:space:]]+(-a[[:space:]]+)?[^|;&]*('"$GUARDED_BASH"')|sed[[:space:]]+-i[^|;&]*('"$GUARDED_BASH"')|(cp|mv)[[:space:]]+[^|;&]*('"$GUARDED_BASH"')|dd[[:space:]]+[^|;&]*of=[^|;&]*('"$GUARDED_BASH"')|curl[[:space:]]+[^|;&]*-o[[:space:]]*[^|;&]*('"$GUARDED_BASH"'))'

# Shell-wrapper detection (V-H1): `bash -c '…'` / `sh -c "…"` / `eval '…'`
# (optionally env-prefixed) carries its real command inside a whitespace-bearing
# quoted token that the quote-strip DROPS — the historical bypass, and the very
# idiom template-mcp.json ships. Flags between the shell and -c must be
# `-`-prefixed tokens so an inner `grep -c` can never masquerade as the wrapper.
WRAPPER_RE='(^|[;&|[:space:]])(env[[:space:]]+[^;&|]*)?(bash|sh|zsh)[[:space:]]+(-[^[:space:]]+[[:space:]]+)*-c[[:space:]]|(^|[;&|[:space:]])eval[[:space:]]'

extract_wrapper_payload() { # $1 = command; echoes the text after -c / eval,
  # with ONE layer of matching outer quotes peeled (a payload whose remainder
  # is not a cleanly-quoted unit — e.g. prose in a commit message — fails the
  # peel and then dies harmlessly in the quote-strip on recursion).
  local rest
  rest="$(printf '%s' "$1" \
    | sed -E 's@^.*((^|[;&|[:space:]])(env[[:space:]]+[^;&|]*)?(bash|sh|zsh)[[:space:]]+(-[^[:space:]]+[[:space:]]+)*-c|(^|[;&|[:space:]])eval)[[:space:]]+@@')"
  case "$rest" in
    \'*\') rest="${rest#\'}"; rest="${rest%\'}" ;;
    \"*\") rest="${rest#\"}"; rest="${rest%\"}" ;;
  esac
  printf '%s' "$rest"
}

is_guarded_write() { # $1 = command text, $2 = recursion depth. 0 = writes a
  # guarded file (directly, or inside a peeled wrapper payload).
  local c="$1" depth="${2:-0}" s payload
  [ "$depth" -ge 3 ] && return 1
  s="$(strip_quotes "$c")"
  if printf '%s' "$s" | grep -qE "$WRITE_RE"; then
    return 0
  fi
  if printf '%s' "$c" | grep -qE "$WRAPPER_RE"; then
    payload="$(extract_wrapper_payload "$c")"
    if [ -n "$payload" ] && [ "$payload" != "$c" ]; then
      is_guarded_write "$payload" $((depth + 1)) && return 0
    fi
  fi
  return 1
}

# Only commands that WRITE onto a guarded file (see WRITE_RE / WRAPPER_RE) are
# candidates; everything else exits here.
is_guarded_write "$cmd" 0 || exit 0

# Scan the RAW command (secrets usually sit inside quotes the strip removed).
leak="$(leak_in "$cmd")"
if [ -n "$leak" ]; then
  deny "(bash-command writing a guarded config file)" "$leak"
  exit 0
fi

# Staged-source scan (M4): `cp/mv <staged> <guarded>` carries the secret in the
# STAGED FILE, not the command text — and it is the default agent reflex after
# a denied direct write ("write to /tmp, then mv into place"). Extract the
# source path and content-scan its head (bounded 256 KiB). Fail-OPEN when the
# source is absent/unreadable (relative paths resolve against an unknown hook
# cwd; a broken guard must never wedge edits).
stripped="$(strip_quotes "$cmd")"
cp_line="$(printf '%s' "$stripped" | grep -E '(^|[;&|[:space:]])(cp|mv)[[:space:]]+[^|;&]*('"$GUARDED_BASH"')' | head -n1 || true)"
if [ -n "$cp_line" ]; then
  src="$(printf '%s' "$cp_line" | sed -E 's@^.*(^|[;&|[:space:]])(cp|mv)[[:space:]]+(-[^[:space:]]+[[:space:]]+)*([^[:space:]]+).*$@\4@')"
  if [ -n "$src" ] && [ -f "$src" ] && [ -r "$src" ]; then
    staged_leak="$(leak_in "$(head -c 262144 "$src" 2>/dev/null || true)")"
    if [ -n "$staged_leak" ]; then
      deny "(bash cp/mv of staged file $src onto a guarded config)" "$staged_leak"
      exit 0
    fi
  fi
fi
exit 0
