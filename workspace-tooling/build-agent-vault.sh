#!/usr/bin/env bash
#
# build-agent-vault.sh
# --------------------
# Builds/refreshes a clean Obsidian vault made entirely of symlinks, so
# Obsidian only ever walks the markdown we care about — never node_modules /
# .git / .pyc.
#
# Why: the dir is a 262k-file monorepo container. Obsidian builds its
# metadata cache by walking the ENTIRE vault tree (the "Loading Cache..."
# phase) and has no per-folder skip — userIgnoreFilters only hides files, it
# does not stop the scan. It also ignores dot-directories by default, so the
# useful .claude/ agent + memory notes were never indexed anyway.
#
# This mirrors only policy-allowlisted Markdown into AgentDocs/ as symlinks.
# Legacy top-level, whole-folder, and asset projections are frozen in place.
#
# NO-DELETE TRANSITION: scheduled runs create missing allowlisted aliases and preserve every
# existing vault entry byte-for-byte. Stale, broken, duplicate, and newly excluded aliases are
# reported by --audit-policy but are not removed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/project-map.json"
POLICY="$SCRIPT_DIR/artifact-policy.json"
MODE="sync"
AUDIT_OUTPUT=""

usage() {
  cat <<'EOF'
Usage: build-agent-vault.sh [--audit-policy] [--policy PATH] [--output PATH]

Default scheduled mode creates missing allowlisted aliases and preserves every
existing vault entry. It performs no unlink, replacement, stale pruning, or
empty-directory deletion.

--audit-policy  Print the exact read-only AgentDocs policy audit as JSON.
--policy PATH   Use an alternate artifact policy.
--output PATH   With --audit-policy, atomically save a new JSON report here.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --audit-policy)
      MODE="audit"
      shift
      ;;
    --policy)
      [ "$#" -ge 2 ] || { echo "--policy requires a path" >&2; exit 2; }
      POLICY="$2"
      shift 2
      ;;
    --output)
      [ "$#" -ge 2 ] || { echo "--output requires a path" >&2; exit 2; }
      AUDIT_OUTPUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[ -f "$POLICY" ] || { echo "artifact policy not found: $POLICY" >&2; exit 2; }
[ "$MODE" = "audit" ] || [ -z "$AUDIT_OUTPUT" ] || {
  echo "--output requires --audit-policy" >&2
  exit 2
}

# The script location is the stable default. Canonical overrides make relocation explicit;
# legacy names remain accepted so existing ad-hoc commands do not break during the transition.
workspace_default="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SRC="${PERSONAL_WORKSPACE_ROOT:-${PERSONAL_SOURCE_ROOT:-$workspace_default}}"
VAULT="${PERSONAL_VAULT_ROOT:-${PERSONAL_PRESENTATION_VAULT_ROOT:-${PERSONAL_OBSIDIAN_VAULT:-$SRC/Vault}}}"
SRC="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$SRC")"
VAULT="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$VAULT")"

if [ "$SRC" = "$VAULT" ]; then
  echo "workspace and presentation vault must be different paths" >&2
  exit 2
fi

if [ "$MODE" = "audit" ]; then
  if [ -n "$AUDIT_OUTPUT" ]; then
    exec python3 "$SCRIPT_DIR/vault_projection_policy.py" \
      --policy "$POLICY" --workspace "$SRC" --vault "$VAULT" \
      audit --output "$AUDIT_OUTPUT"
  fi
  exec python3 "$SCRIPT_DIR/vault_projection_policy.py" \
    --policy "$POLICY" --workspace "$SRC" --vault "$VAULT" audit
fi

# Default-deny AgentDocs farm. Legacy clean-folder, top-level Markdown, and
# asset projections are frozen: existing entries remain untouched, but normal
# scheduled runs no longer create new aliases outside this policy plan.
FARM="$VAULT/AgentDocs"
desired="$(mktemp)"
farm_plan="$(mktemp)"
dedupe_exclusions="$(mktemp)"
actual="$(mktemp)"
lock_dir=""
cleanup() {
  rm -f "$desired" "$farm_plan" "$dedupe_exclusions" "$actual"
  if [ -n "$lock_dir" ] && [ -d "$lock_dir" ]; then
    rm -f "$lock_dir/pid"
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Validate and construct the complete plan before any vault mutation.
python3 "$SCRIPT_DIR/vault_projection_policy.py" \
  --policy "$POLICY" --workspace "$SRC" --vault "$VAULT" plan >"$farm_plan"

# Project-local _sources aliases are the canonical presentation paths.  The
# dedupe helper emits only farm destinations that have an exact live canonical
# alias and no indexed or Obsidian-workspace references.  Referenced aliases
# remain in the desired set. Filtering happens before link creation.
python3 "$SCRIPT_DIR/agentdocs_alias_dedupe.py" \
  --manifest "$MANIFEST" \
  --workspace "$SRC" \
  --vault "$VAULT" \
  --farm-plan "$farm_plan" \
  --emit-safe-destinations >"$dedupe_exclusions"
LC_ALL=C sort -u "$dedupe_exclusions" -o "$dedupe_exclusions"
suppressed="$(wc -l <"$dedupe_exclusions" | tr -d ' ')"

awk -F '\t' \
  'FILENAME == ARGV[1] { excluded[$0] = 1; next } !($2 in excluded) { print $0 }' \
  "$dedupe_exclusions" "$farm_plan" >"$desired"

if [ ! -e "$VAULT" ] || [ -L "$VAULT" ] || [ ! -d "$VAULT" ]; then
  echo "presentation vault must be an existing real directory: $VAULT" >&2
  exit 2
fi
if [ -L "$FARM" ] || { [ -e "$FARM" ] && [ ! -d "$FARM" ]; }; then
  echo "AgentDocs must be a real directory when present: $FARM" >&2
  exit 2
fi

# Serialize scheduled invocations. The lock lives in temporary runtime state,
# not in the source workspace or presentation vault.
lock_id="$(printf '%s' "$VAULT" | shasum -a 256 | awk '{print $1}')"
lock_dir="${TMPDIR:-/tmp}/workspace-vault-${lock_id}.lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "another agent-vault sync is already running: $lock_dir" >&2
  exit 2
fi
printf '%s\n' "$$" >"$lock_dir/pid"

created=0
preserved=0
collisions=0
while IFS=$'\t' read -r target dest; do
  [ -n "$target" ] || continue
  status="$(python3 "$SCRIPT_DIR/safe_vault_link.py" \
    --workspace "$SRC" --vault "$VAULT" \
    --target "$target" --destination "$dest")"
  case "$status" in
    created) created=$((created + 1)) ;;
    preserved) preserved=$((preserved + 1)) ;;
    collision)
      collisions=$((collisions + 1))
      echo "preserving existing destination collision: $dest" >&2
      ;;
    *)
      echo "unexpected safe-link result for $dest: $status" >&2
      exit 2
      ;;
  esac
done <"$desired"

# Calculate the existing-vs-desired delta for reporting only. No entry is
# unlinked, replaced, repointed, or pruned.
cut -f 2 "$desired" | LC_ALL=C sort -u -o "$desired"
if [ -d "$FARM" ] && [ ! -L "$FARM" ]; then
  find "$FARM" -type l 2>/dev/null | LC_ALL=C sort -u >"$actual"
else
  : >"$actual"
fi
existing_farm="$(wc -l <"$actual" | tr -d ' ')"
outside_allowlist="$(LC_ALL=C comm -23 "$actual" "$desired" | wc -l | tr -d ' ')"

echo "$(date '+%Y-%m-%d %H:%M:%S')  agent-vault sync: ${created} linked, 0 deleted, ${existing_farm} existing AgentDocs aliases preserved, ${outside_allowlist} outside current allowlist, ${suppressed} new duplicate aliases suppressed, ${collisions} collisions preserved"
