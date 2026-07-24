#!/bin/bash
set -euo pipefail

########################################################################
# test-smoke.sh — Quick MCP server smoke test
#
# Sends JSON-RPC requests to the server via stdin pipe and validates
# responses. Uses a simple sequential approach (no named pipes).
########################################################################

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_JS="$SCRIPT_DIR/dist/index.js"

# Detect PLATFORM_ROOT
if [[ -z "${PLATFORM_ROOT:-}" ]]; then
  dir="$SCRIPT_DIR"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/charts" && -d "$dir/sandbox" ]]; then
      PLATFORM_ROOT="$dir"
      break
    fi
    dir="$(dirname "$dir")"
  done
fi

if [[ -z "${PLATFORM_ROOT:-}" ]]; then
  echo -e "${RED}Cannot detect PLATFORM_ROOT${RESET}"
  exit 1
fi

export PLATFORM_ROOT
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-$PLATFORM_ROOT}"

# Build if needed
if [[ ! -f "$SERVER_JS" ]]; then
  echo "Building..."
  (cd "$SCRIPT_DIR" && npm run build 2>&1) || { echo -e "${RED}Build failed${RESET}"; exit 1; }
fi

echo -e "${CYAN}${BOLD}=== workspace MCP Server Smoke Test ===${RESET}"
echo "  PLATFORM_ROOT : $PLATFORM_ROOT"
echo "  WORKSPACE_ROOT: $WORKSPACE_ROOT"
echo

PASS=0
FAIL=0

# NB: `PASS=$((PASS+1))`, not `((PASS++))` — under `set -e` the post-increment
# form evaluates to the (falsy) pre-increment value 0 on the first call and
# aborts the whole script. The assignment form always returns success.
pass() { echo -e "  ${GREEN}PASS${RESET} $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${RESET} $1"; FAIL=$((FAIL+1)); }

# Helper: send a single request and capture the response
test_request() {
  local label="$1"
  local request="$2"
  local expected="$3"

  local response
  response=$(echo "$request" | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

  if [[ -z "$response" ]]; then
    fail "$label — no response"
    return 1
  fi

  if echo "$response" | grep -q "$expected"; then
    pass "$label"
    return 0
  else
    fail "$label — expected '$expected' in response"
    echo "    Response (first 200 chars): ${response:0:200}"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Test 1: initialize
# ---------------------------------------------------------------------------
echo -e "${BOLD}Test 1: initialize${RESET}"
test_request "initialize returns server name" \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '@chris-dare-dev/agent-kit'

# ---------------------------------------------------------------------------
# Test 2: tools/list (sends initialize + notification + list in sequence)
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}Test 2: tools/list${RESET}"
TOOLS_RESPONSE=$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

for tool in list_skills get_environment_map get_app_context search_platform_knowledge list_agents get_agent; do
  if echo "$TOOLS_RESPONSE" | grep -q "\"$tool\""; then
    pass "tools/list contains '$tool'"
  else
    fail "tools/list missing '$tool'"
  fi
done

# ---------------------------------------------------------------------------
# Test 3: get_environment_map
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}Test 3: get_environment_map${RESET}"
ENV_RESPONSE=$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_environment_map","arguments":{"env":"dev"}}}' \
  | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

if echo "$ENV_RESPONSE" | grep -q "000000000000"; then
  pass "get_environment_map returns dev account 000000000000"
else
  fail "get_environment_map — expected 000000000000"
fi

# ---------------------------------------------------------------------------
# Test 4: list_skills
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}Test 4: list_skills${RESET}"
SKILLS_RESPONSE=$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_skills","arguments":{}}}' \
  | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

if echo "$SKILLS_RESPONSE" | grep -q "argocd-debug"; then
  pass "list_skills contains argocd-debug"
else
  fail "list_skills — expected argocd-debug"
fi

# ---------------------------------------------------------------------------
# Test 5: search_platform_knowledge
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}Test 5: search_platform_knowledge${RESET}"
SEARCH_RESPONSE=$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_platform_knowledge","arguments":{"query":"ArgoCD sync wave","max_results":3}}}' \
  | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

if echo "$SEARCH_RESPONSE" | grep -q "result"; then
  pass "search_platform_knowledge returns results"
else
  fail "search_platform_knowledge — no results"
fi

# ---------------------------------------------------------------------------
# Test 6: list_agents
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}Test 6: list_agents${RESET}"
AGENTS_RESPONSE=$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_agents","arguments":{}}}' \
  | PLATFORM_ROOT="$PLATFORM_ROOT" WORKSPACE_ROOT="$WORKSPACE_ROOT" timeout 15 node "$SERVER_JS" 2>/dev/null || true)

if echo "$AGENTS_RESPONSE" | grep -q "cluster-health"; then
  pass "list_agents contains cluster-health"
else
  fail "list_agents — expected cluster-health"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo -e "${BOLD}=== Results: ${GREEN}${PASS} passed${RESET}${BOLD}, ${RED}${FAIL} failed${RESET}${BOLD} ===${RESET}"
[[ "$FAIL" -eq 0 ]] || exit 1
