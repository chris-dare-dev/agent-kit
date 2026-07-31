#!/usr/bin/env bash
# Provision the artifact-memory substrate inside a WSL2 guest.
#
# Runs in the GUEST. Every step is idempotent and every step that WRITES is
# behind --apply; without it this is a read-only plan, which is the same
# contract artifact_memory_provision.py already has.
#
# Exit: 0 planned or applied cleanly · 1 a prerequisite is missing or a step failed.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

APPLY=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY="--apply" ;;
    -h|--help)
      sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "setup: unknown argument: $arg" >&2; exit 1 ;;
  esac
done

VENV="$HOME/.local/share/agent-kit/venv"
LOCKFILE="workspace-tooling/requirements-artifact-ingestion.lock.txt"

fail() { echo "setup: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

step "prerequisites"
command -v python3 >/dev/null || fail "python3 is not installed. sudo apt install python3 python3-venv"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "  python3      $PYV"
[ "$(printf '%s\n3.12\n' "$PYV" | sort -V | head -1)" = "3.12" ] \
  || echo "  WARNING: the lockfile is pinned for Python 3.12; $PYV may resolve differently"

command -v docker >/dev/null || fail "docker is not installed in this guest. Enable WSL integration in Docker Desktop, or install docker.io."
docker info >/dev/null 2>&1 || fail "docker is installed but not running. Start Docker Desktop (with WSL integration) or 'sudo service docker start'."
echo "  docker       running"

# The repo must be on the LINUX filesystem. See docs/platforms/windows-wsl.md:
# /mnt/c is a 9p mount, where file locking is not the same primitive the
# substrate relies on and I/O is roughly an order of magnitude slower.
case "$REPO_ROOT" in
  /mnt/*)
    fail "this clone is on the Windows filesystem ($REPO_ROOT).
  The substrate takes advisory file locks and does heavy small-file I/O; /mnt/c is
  a 9p mount where neither behaves like a native filesystem. Clone into the WSL
  filesystem instead:
      git clone <url> ~/agent-kit && cd ~/agent-kit
  See docs/platforms/windows-wsl.md." ;;
esac
echo "  repo         $REPO_ROOT (Linux filesystem)"

step "python virtualenv"
if [ -x "$VENV/bin/python" ]; then
  echo "  present      $VENV"
else
  if [ -z "$APPLY" ]; then
    echo "  would create $VENV (re-run with --apply)"
  else
    python3 -m venv "$VENV" || fail "could not create the venv. sudo apt install python3-venv"
    echo "  created      $VENV"
  fi
fi

if [ -n "$APPLY" ] && [ -x "$VENV/bin/python" ]; then
  echo "  installing   $LOCKFILE"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip \
    || fail "pip self-upgrade failed"
  "$VENV/bin/python" -m pip install --quiet --require-hashes -r "$LOCKFILE" \
    || fail "dependency install failed. The lockfile is hash-pinned; a mirror that rewrites artifacts will fail here by design."
  echo "  installed"
fi

step "substrate provisioning"
# Prefer the venv, but only if it exists -- invoking a missing interpreter and
# letting it fail prints a confusing "No such file or directory" ahead of the
# output that matters. Provisioning itself is stdlib-only, so python3 is fine.
if [ -x "$VENV/bin/python" ]; then
  PROVISION_PY="$VENV/bin/python"
else
  PROVISION_PY="python3"
  echo "  (using python3; the venv is only needed for queries, not provisioning)"
fi
PYTHONPATH="$REPO_ROOT/workspace-tooling" \
  "$PROVISION_PY" workspace-tooling/artifact_memory_provision.py $APPLY \
  || fail "provisioning failed"

step "next"
if [ -z "$APPLY" ]; then
  cat <<EOF
  That was a PLAN. Nothing was written.
  Re-run with --apply to create the venv, install the pinned dependencies and
  start the Qdrant container:

      scripts/wsl/setup.sh --apply

EOF
else
  cat <<EOF
  Provisioned. Verify the whole path end to end:

      scripts/wsl/smoke.sh

  From the Windows host instead:

      node scripts/wsl-smoke.mjs

EOF
fi
