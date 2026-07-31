#!/usr/bin/env bash
# Round-trip one memory query through the resident service, inside WSL2.
#
# Runs in the GUEST. `scripts/wsl-smoke.mjs` is the Windows-host driver that
# checks for WSL2 and invokes this. Running it directly on Linux is fine too --
# nothing here is WSL-specific, which is the point: the WSL2 path is ordinary
# Linux, not a special build.
#
# Prints the socket path and the derived-state root before doing anything, so a
# failure tells you WHERE it was looking. Always shuts the service down.
#
# Exit: 0 the query round-tripped · 1 a step failed · 3 the substrate is not
# provisioned (a precondition, not a defect -- run scripts/wsl/setup.sh).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PY="${AGENT_KIT_PYTHON:-}"
if [ -z "$PY" ]; then
  # The provisioned venv first: ~30 substrate paths import qdrant_client at call
  # time, and a memory query is one of them.
  if [ -x "$HOME/.local/share/agent-kit/venv/bin/python" ]; then
    PY="$HOME/.local/share/agent-kit/venv/bin/python"
  else
    PY="python3"
  fi
fi

export PYTHONPATH="$REPO_ROOT/workspace-tooling${PYTHONPATH:+:$PYTHONPATH}"

DERIVED_ROOT="$("$PY" -c 'import artifact_runtime; print(artifact_runtime.derived_root())' 2>/dev/null)"
if [ -z "$DERIVED_ROOT" ]; then
  echo "smoke: cannot resolve the derived-state root -- is PYTHONPATH right?" >&2
  exit 1
fi
SOCKET_PATH="$DERIVED_ROOT/services/qdrant/artifact-memory.sock"
RUNTIME_CONFIG="$DERIVED_ROOT/artifact-memory-runtime.json"

echo "smoke: interpreter    $PY"
echo "smoke: derived root   $DERIVED_ROOT"
echo "smoke: socket path    $SOCKET_PATH"

if [ ! -f "$RUNTIME_CONFIG" ]; then
  cat >&2 <<EOF
smoke: the substrate is not provisioned on this guest.
  missing: $RUNTIME_CONFIG
  Run:     scripts/wsl/setup.sh
  This is a precondition, not a failure of the code under test.
EOF
  exit 3
fi

cleanup() {
  if [ -n "${SERVICE_PID:-}" ] && kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
    echo "smoke: service stopped"
  fi
}
trap cleanup EXIT

"$PY" workspace-tooling/artifact_memory_service.py &
SERVICE_PID=$!

# Wait for the socket rather than sleeping a guessed interval.
for _ in $(seq 1 100); do
  [ -S "$SOCKET_PATH" ] && break
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    echo "smoke: the service exited before it listened" >&2
    exit 1
  fi
  sleep 0.2
done
if [ ! -S "$SOCKET_PATH" ]; then
  echo "smoke: no socket at $SOCKET_PATH after 20s" >&2
  exit 1
fi
echo "smoke: service listening"

# One real memory query over the Unix socket. python3 rather than
# `curl --unix-socket`, because python3 is a hard dependency here and curl is not.
"$PY" - "$SOCKET_PATH" <<'PYEOF'
import json, socket, sys

sock_path = sys.argv[1]
body = json.dumps({"query": "agent kit", "limit": 3}).encode()
request = (
    b"POST /v1/search HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    b"Connection: close\r\n\r\n" + body
)

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(sock_path)
s.sendall(request)
chunks = []
while True:
    part = s.recv(65536)
    if not part:
        break
    chunks.append(part)
s.close()

raw = b"".join(chunks)
head, _, payload = raw.partition(b"\r\n\r\n")
status = head.split(b"\r\n", 1)[0].decode(errors="replace")
print(f"smoke: response      {status}")

if b" 200 " not in head.split(b"\r\n", 1)[0]:
    sys.stderr.write("smoke: query did not return 200\n")
    sys.stderr.write(payload.decode(errors="replace")[:600] + "\n")
    raise SystemExit(1)

# Structured, and non-empty in the sense that matters: it parsed as JSON and
# carries the result key. An empty corpus legitimately returns zero hits, so the
# assertion is on SHAPE, not on hit count -- asserting hits would make this fail
# on a correctly-working empty index.
parsed = json.loads(payload)
if not isinstance(parsed, dict):
    raise SystemExit("smoke: response was not a JSON object")
if "results" not in parsed:
    raise SystemExit(f"smoke: no 'results' key; got {sorted(parsed)[:8]}")
print(f"smoke: results       {len(parsed['results'])} hit(s)")
print("smoke: structured result OK")
PYEOF
QUERY_RC=$?

if [ "$QUERY_RC" -ne 0 ]; then
  echo "smoke: FAILED" >&2
  exit 1
fi
echo "smoke: PASS - one memory query round-tripped over $SOCKET_PATH"
exit 0
