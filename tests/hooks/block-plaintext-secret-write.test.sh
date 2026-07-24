#!/usr/bin/env bash
# Test harness for data/hooks/block-plaintext-secret-write.sh — codifies the
# deny/allow cases so future regex edits can't regress silently. Requires jq
# (the hook needs it too); SKIPs cleanly if jq is unavailable so a jq-less
# environment does not red the gate.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/../../data/hooks/block-plaintext-secret-write.sh"
[ -f "$HOOK" ] || { echo "FAIL: hook not found at $HOOK" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "SKIP: jq unavailable — hook test not run"; exit 0; }

# Fake tokens — correct SHAPE only, not real secrets.
ATATT="ATATT3xFfGF0abcdef1234567890ABCDEF"
SKANT="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
GLPAT="glpat-AAAAAAAAAAAAAAAAAAAAAAAA"

fails=0
check() { # label want(DENY|ALLOW) file_path field(content|new_string) value
  local label="$1" want="$2" fp="$3" field="$4" val="$5" out got
  out="$(jq -n --arg fp "$fp" --arg f "$field" --arg v "$val" \
    '{tool_input: ({file_path:$fp} + {($f):$v})}' | bash "$HOOK" 2>/dev/null || true)"
  if printf '%s' "$out" | grep -q '"permissionDecision": "deny"'; then got=DENY; else got=ALLOW; fi
  if [ "$got" = "$want" ]; then echo "ok   $label -> $got"; else
    echo "FAIL $label -> $got (want $want)" >&2; fails=$((fails+1)); fi
}

# Staged-file fixtures for the cp/mv source-content scan (rectification M4).
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
printf '{"api_token": "%s"}\n' "$GLPAT" > "$TMPD/staged-secret.json"
printf '{"CONFLUENCE_API_TOKEN": "${CONFLUENCE_API_TOKEN}"}\n' > "$TMPD/staged-clean.json"

check "ATATT in .mcp.json"          DENY  "/w/.mcp.json"           content    "{\"env\":{\"X\":\"$ATATT\"}}"
check "sk-ant in .mcp.json"         DENY  "/w/.mcp.json"           content    "{\"env\":{\"X\":\"$SKANT\"}}"
check "glpat in .claude.json"       DENY  "/home/u/.claude.json"   content    "{\"env\":{\"X\":\"$GLPAT\"}}"
check "generic SECRET in .mcp.json" DENY  "/w/.mcp.json"           content    "{\"env\":{\"MY_SECRET\":\"hunter2hunter2hunter2\"}}"
check "plaintext in .codex toml"    DENY  "/w/.codex/config.toml"  new_string "CONFLUENCE_API_TOKEN = \"$ATATT\""
check "indirection in .mcp.json"    ALLOW "/w/.mcp.json"           content    "{\"env\":{\"CONFLUENCE_API_TOKEN\":\"\${CONFLUENCE_API_TOKEN}\"}}"
check "url+user in .codex toml"     ALLOW "/w/.codex/config.toml"  new_string "CONFLUENCE_URL = \"https://x\""
check "token in non-guarded file"   ALLOW "/w/src/foo.ts"         content    "const t=\"$ATATT\""
# Rectification M1 — the 2026-07-12 incident's own registration files are guarded.
check "glpat in ~/.claude/settings.json"  DENY  "/Users/u/.claude/settings.json" content "{\"env\":{\"X\":\"$GLPAT\"}}"
check "glpat in claude_desktop_config"    DENY  "/Users/u/Library/Application Support/Claude/claude_desktop_config.json" content "{\"t\":\"$GLPAT\"}"
check "hooks cfg in settings.json OK"     ALLOW "/Users/u/.claude/settings.json" content "{\"hooks\":{\"PreToolUse\":[{\"matcher\":\"Bash\"}]}}"
check "theme in desktop config OK"        ALLOW "/Users/u/Library/Application Support/Claude/claude_desktop_config.json" content "{\"theme\":\"dark\"}"

# --- Bash-command branch (S1.2) — same payload contract for Claude AND Codex
# (both send PreToolUse JSON with .tool_input.command; SP1 measured them
# identical), so these cases cover both providers' Bash matchers.
check_bash() { # label want(DENY|ALLOW) command
  local label="$1" want="$2" cmd="$3" out got
  out="$(jq -n --arg c "$cmd" '{tool_input:{command:$c}}' | bash "$HOOK" 2>/dev/null || true)"
  if printf '%s' "$out" | grep -q '"permissionDecision": "deny"'; then got=DENY; else got=ALLOW; fi
  if [ "$got" = "$want" ]; then echo "ok   $label -> $got"; else
    echo "FAIL $label -> $got (want $want)" >&2; fails=$((fails+1)); fi
}

check_bash "bash: redirect glpat > .mcp.json"    DENY  "echo '$GLPAT' > .mcp.json"
check_bash "bash: append >> .claude.json"        DENY  "echo \"X=$ATATT\" >> /home/u/.claude.json"
check_bash "bash: tee onto .mcp.json"            DENY  "printf '%s' '$SKANT' | tee /w/.mcp.json"
check_bash "bash: sed -i into codex toml"        DENY  "sed -i 's/OLD/$GLPAT/' .codex/config.toml"
check_bash "bash: quoted redirect target"        DENY  "echo \"$GLPAT\" > \".mcp.json\""
check_bash "bash: heredoc-style cat write"       DENY  "cat > .mcp.json <<EOF
{\"env\":{\"T\":\"$ATATT\"}}
EOF"
check_bash "bash: indirection write allowed"     ALLOW "printf '%s' '{\"CONFLUENCE_API_TOKEN\":\"\${CONFLUENCE_API_TOKEN}\"}' > .mcp.json"
check_bash "bash: token to non-guarded file"     ALLOW "echo '$ATATT' > /tmp/scratch.txt"
check_bash "bash: read-only cat of guarded"      ALLOW "cat .mcp.json"
check_bash "bash: commit msg mentions redirect"  ALLOW "git commit -m \"docs: explain why echo $GLPAT > .mcp.json is blocked\""
check_bash "bash: benign non-secret command"     ALLOW "ls -la && git status"
check_bash "bash: cp template without secret"    ALLOW "cp template.json .mcp.json"

# --- V-H1 rectification: wrapper recursion + widened write-verb set ------------
check_bash "bash: bash -c wrapper (sq payload)"  DENY  "bash -c 'echo $GLPAT > .mcp.json'"
check_bash "bash: sh -c wrapper (dq payload)"    DENY  "sh -c \"echo $ATATT >> .claude.json\""
check_bash "bash: eval wrapper"                  DENY  "eval 'echo $GLPAT > .mcp.json'"
check_bash "bash: >| clobber redirect"           DENY  "echo '$GLPAT' >| .mcp.json"
check_bash "bash: dd of= guarded"                DENY  "printf '%s' '$SKANT' | dd of=.mcp.json"
check_bash "bash: curl -o guarded"               DENY  "curl -H 'PRIVATE-TOKEN: $GLPAT' -o .mcp.json https://gitlab/api"
check_bash "bash: write to user settings.json"   DENY  "echo '$GLPAT' > /Users/u/.claude/settings.json"
check_bash "bash: commit msg mentions wrapper"   ALLOW "git commit -m \"docs: why bash -c 'echo $GLPAT > .mcp.json' is denied\""

# --- M4 rectification: cp/mv staged-source content scan ------------------------
check_bash "bash: cp staged SECRET onto guarded" DENY  "cp $TMPD/staged-secret.json .mcp.json"
check_bash "bash: cp staged CLEAN onto guarded"  ALLOW "cp $TMPD/staged-clean.json .mcp.json"

# --- DOCUMENTED PUNTS (codified decision, not silent holes — V-H1/M4): the Bash
# branch deliberately does NOT cover interpreter-level writes. Defense-in-depth:
# the Edit|Write guard path + the repo secret scan (CI Gate 1n / pre-push).
# If a fixture below ever flips to DENY, the coverage comment in the hook header
# must be updated in the same commit.
check_bash "bash: python3 -c write (PUNT)"       ALLOW "python3 -c 'open(\".mcp.json\",\"w\").write(\"$GLPAT\")'"
check_bash "bash: node -e write (PUNT)"          ALLOW "node -e 'require(\"fs\").writeFileSync(\".mcp.json\",\"$GLPAT\")'"

if [ "$fails" -gt 0 ]; then echo "$fails hook case(s) failed" >&2; exit 1; fi
echo "all hook cases passed"
exit 0
