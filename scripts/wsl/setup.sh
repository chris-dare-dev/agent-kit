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
  # Version-pinned, NOT hash-pinned -- and that is the lockfile's own documented
  # design, not an oversight: onnxruntime, numpy, tokenizers and pillow ship
  # per-platform wheels, so a hash-pinned lock captured on one platform will not
  # install on another. Per-platform hash locks are the F-10 follow-up the
  # lockfile header names. Passing --require-hashes here fails outright (pip
  # demands hashes for EVERY requirement once any one has them), and claiming
  # supply-chain integrity this file does not provide would be worse than not
  # claiming it.
  "$VENV/bin/python" -m pip install --quiet -r "$LOCKFILE" \
    || fail "dependency install failed. The lock was captured on darwin/arm64 under
  Python 3.12; a different platform or interpreter may not resolve every pin.
  Check the pip output above, and see the lockfile header."
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

step "corpus bootstrap"
# Provisioning creates the runtime CONFIG. It does not create the derived state
# that config points at, and the resident service refuses to start until every
# referenced path exists (fail-closed, by design). The chain below was
# established empirically -- the repository documents no fresh-install path --
# so this reports status per step rather than guessing on your behalf. The
# catalog step in particular needs a POLICY, which is a real choice.
D="$(PYTHONPATH="$REPO_ROOT/workspace-tooling" python3 -c \
  'import artifact_runtime; print(artifact_runtime.derived_root())' 2>/dev/null)"
VP="$VENV/bin/python"

report() {  # report <path> <what-creates-it>
  if [ -e "$1" ]; then
    echo "  present   $(basename "$1")"
  else
    echo "  MISSING   $(basename "$1")"
    echo "            $2"
  fi
}

if [ -n "$D" ]; then
  report "$D/artifact-catalog.sqlite3" \
    "PYTHONPATH=\$PWD/workspace-tooling $VP workspace-tooling/artifact_catalog.py --workspace \"\$PWD\" --policy workspace-tooling/artifact-policy.example.json"
  report "$D/outbox" \
    "PYTHONPATH=\$PWD/workspace-tooling $VP workspace-tooling/artifact_ingestion.py prepare --catalog $D/artifact-catalog.sqlite3 --output-root $D/outbox"
  report "$D/ingestion-state.sqlite3" \
    "artifact_ingestion.py qdrant --outbox <run-dir> --state $D/ingestion-state.sqlite3 --qdrant-url <url> --collection <name> --apply   (needs QDRANT_API_KEY)"
  report "$D/artifact-event-consumer.sqlite3" \
    "artifact_event_consumer.py consume --state $D/artifact-event-consumer.sqlite3 --no-runtime-config --qdrant-path $D/qdrant --apply"
  report "$D/services/qdrant/build-manifest.json" \
    "NO KNOWN FRESH-INSTALL COMMAND. Only artifact_qdrant_migrate.py backfill writes it, and that is a migration tool that requires completed checkpoints on the embedded backend. See docs/platforms/windows-wsl.md."
fi

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
  The venv, the Qdrant container and the runtime config are in place.

  If every line under "corpus bootstrap" says present, verify end to end:

      scripts/wsl/smoke.sh          # or, from Windows: node scripts/wsl-smoke.mjs

  If any says MISSING, run the command shown beneath it. The build-manifest step
  currently has no known fresh-install command -- that gap is documented in
  docs/platforms/windows-wsl.md and is not something this script can paper over.

EOF
fi
