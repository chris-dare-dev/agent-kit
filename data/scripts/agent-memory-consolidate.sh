#!/usr/bin/env bash
# agent-memory-consolidate.sh — symlink-farm consolidation of fragmented agent-memory roots
# (E2 / self-improving-tooling-m2, S2.1 migration).
#
# WHAT IT DOES
#   Workspace agent-memory is fragmented across ~40+ live cwd-relative `.claude/agent-memory`
#   directories (one per repo + sub-dir noise; the exact count drifts as pipelines touch new
#   repos), each holding a DIVERGENT copy of each agent's
#   lessons. This script consolidates them to ONE unified root so the EXISTING hardcoded
#   cwd-relative reads/writes in the 59 agent bodies transparently hit the unified store —
#   WITHOUT editing a single agent body. For each legacy root it:
#     1. BACKS UP the whole root to a tarball (before any mutation),
#     2. MERGES its per-agent *.md corpus INTO the unified root via concat + exact-line dedup
#        (lossless — never drops a distinct lesson),
#     3. REPLACES the legacy dir with a SYMLINK to the unified root.
#   After this, `cat .claude/agent-memory/<name>/lessons.md` from any repo resolves THROUGH the
#   symlink to the single unified corpus, and `>> ...lessons.md` accumulates into it (the E3 goal).
#
# SAFETY
#   * DRY-RUN by default. Pass --apply to mutate. (Dry-run prints the plan, touches nothing.)
#   * BACKUP before mutate. Every legacy root is tarred into --backup-dir first.
#   * IDEMPOTENT. Re-running is a no-op: roots already symlinked to the unified root are skipped,
#     and exact-line dedup is set-union (merging the same content twice changes nothing).
#   * The unified root itself, and any root nested INSIDE the unified root, are never symlinked
#     into themselves (loop guard).
#   * stdlib/bash + awk + tar + find only — no new deps. macOS-friendly (BSD stat/find).
#
# USAGE
#   agent-memory-consolidate.sh [--apply] [--root <unified>] [--workspace <dir>]
#                               [--backup-dir <dir>] [--quiet]
#   --apply            actually mutate (default is dry-run)
#   --root <dir>       unified root. Default: $AGENT_MEMORY_ROOT, else
#                      $WORKSPACE_ROOT/.claude/agent-memory, else ./.claude/agent-memory
#   --workspace <dir>  the `find` scope for discovering legacy roots.
#                      Default: $WORKSPACE_ROOT, else the dir above the unified root, else "."
#   --backup-dir <dir> where per-root tarballs are written.
#                      Default: <unified-root>/.consolidate-backups/<UTC-timestamp>/
#   --quiet            suppress per-step log lines (errors still print)
#
# EXIT: 0 on success (including dry-run and no-op idempotent re-run), non-zero on error.

set -euo pipefail

# ---- resolution helper (shared with agent-memory-root.sh) -------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=agent-memory-root.sh
AMR_SOURCED_ONLY=1 . "$HERE/agent-memory-root.sh"

APPLY=0
QUIET=0
UNIFIED=""
WORKSPACE=""
BACKUP_DIR=""

log()  { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)       APPLY=1; shift ;;
    --quiet)       QUIET=1; shift ;;
    --root)        UNIFIED="${2:?--root needs a value}"; shift 2 ;;
    --workspace)   WORKSPACE="${2:?--workspace needs a value}"; shift 2 ;;
    --backup-dir)  BACKUP_DIR="${2:?--backup-dir needs a value}"; shift 2 ;;
    -h|--help)     awk 'NR>1 && /^[^#]/{exit} NR>1{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *)             err "unknown arg: $1"; exit 2 ;;
  esac
done

# ---- resolve unified root + workspace scope ---------------------------------------------
if [ -z "$UNIFIED" ]; then
  UNIFIED="$(agent_memory_base)"
fi
# Canonicalize the unified root path WITHOUT requiring it to exist yet.
_canon() {
  # Echo an absolute, symlink-free-as-possible path for $1 (dir may not exist yet).
  d="$1"
  case "$d" in
    /*) : ;;
    *)  d="$(pwd)/$d" ;;
  esac
  # Resolve the existing-prefix with cd -P; append the missing tail.
  _existing="$d"; _tail=""
  while [ ! -d "$_existing" ] && [ "$_existing" != "/" ] && [ -n "$_existing" ]; do
    _tail="$(basename "$_existing")${_tail:+/$_tail}"
    _existing="$(dirname "$_existing")"
  done
  if [ -d "$_existing" ]; then
    _existing="$(cd "$_existing" && pwd -P)"
  fi
  printf '%s\n' "${_existing%/}${_tail:+/$_tail}"
}
UNIFIED="$(_canon "$UNIFIED")"

if [ -z "$WORKSPACE" ]; then
  if [ -n "${WORKSPACE_ROOT:-}" ]; then
    WORKSPACE="$WORKSPACE_ROOT"
  else
    # default: the directory two levels above the unified root (…/<ws>/.claude/agent-memory)
    WORKSPACE="$(dirname "$(dirname "$UNIFIED")")"
  fi
fi
WORKSPACE="$(_canon "$WORKSPACE")"

if [ -z "$BACKUP_DIR" ]; then
  BACKUP_DIR="$UNIFIED/.consolidate-backups/$(date -u +%Y%m%dT%H%M%SZ)"
fi

MODE="DRY-RUN (no mutation; pass --apply to mutate)"
[ "$APPLY" = "1" ] && MODE="APPLY (mutating)"
log "agent-memory-consolidate  [$MODE]"
log "  unified root : $UNIFIED"
log "  workspace    : $WORKSPACE"
log "  backup dir   : $BACKUP_DIR"
log ""

# ---- helpers -----------------------------------------------------------------------------

# inode of a path (BSD stat on macOS, GNU stat elsewhere); empty if missing.
_inode() { stat -f %i "$1" 2>/dev/null || stat -c %i "$1" 2>/dev/null || true; }

# Is $1 a symlink whose canonical target equals the unified root?
_is_symlink_to_unified() {
  [ -L "$1" ] || return 1
  # readlink may be relative; resolve relative to the link's parent.
  case "$(readlink "$1")" in
    /*) tgt="$(_canon "$(readlink "$1")")" ;;
    *)  tgt="$(_canon "$(dirname "$1")/$(readlink "$1")")" ;;
  esac
  [ "$tgt" = "$UNIFIED" ]
}

# CONCAT + DEDUP merge of all source files into one dest, provably lossless for distinct lines.
# Args: <dest-file> <src-file>...   (existing dest is included as the FIRST input so its lines
# keep precedence/order). Exact-line dedup after trailing-whitespace trim; first-seen order
# preserved; runs of blank lines collapsed to one; only byte-identical lines are ever collapsed
# (two lessons differing by one char BOTH survive — the "never drop a lesson" lock).
_merge_files() {
  _dest="$1"; shift
  _tmp="$(mktemp "${TMPDIR:-/tmp}/amc.XXXXXX")"
  {
    [ -f "$_dest" ] && cat "$_dest"
    for f in "$@"; do [ -f "$f" ] && cat "$f"; done
  } | awk '
      { sub(/[ \t]+$/, "", $0) }                 # trim trailing whitespace
      /^[[:space:]]*$/ {                          # blank line: collapse runs
        if (!blank) { print ""; blank=1 } next
      }
      { blank=0; if (!seen[$0]++) print $0 }      # exact-line dedup, first-seen order
    ' > "$_tmp"
  mkdir -p "$(dirname "$_dest")"
  mv "$_tmp" "$_dest"
}

# ---- enumerate legacy roots --------------------------------------------------------------
# All real `.claude/agent-memory` directories under the workspace, excluding node_modules.
# We process even ones nested inside the unified root's parent, but SKIP the unified root
# itself and any root that lies inside the unified root (loop guard), and skip ones already
# symlinked to unified (idempotency — counted as `already-symlinked`, checked BEFORE canon so
# a re-run does not mis-resolve a migrated symlink as the unified root itself).
# Output is `sort`ed so the dry-run plan is deterministic and diffable across runs (the raw
# `find` traversal order is not stable, esp. while the tree is being mutated mid-run).
ROOTS_TMP="$(mktemp "${TMPDIR:-/tmp}/amc-roots.XXXXXX")"
# -name agent-memory, type d OR symlink (so we can detect+skip already-migrated symlinks).
find "$WORKSPACE" \( -type d -o -type l \) -name agent-memory \
     -not -path '*/node_modules/*' 2>/dev/null \
  | grep -E '/\.claude/agent-memory$' | sort > "$ROOTS_TMP" || true

TOTAL=0; MIGRATED=0; SKIPPED=0; ALREADY=0
PROCESSED_MARK="$(mktemp "${TMPDIR:-/tmp}/amc-done.XXXXXX")"  # for the inode-verify summary

while IFS= read -r LEGACY; do
  [ -n "$LEGACY" ] || continue
  TOTAL=$((TOTAL + 1))

  # Idempotency FIRST, BEFORE canonicalizing: a legacy root that is already a symlink to the
  # unified root would have `_canon "$LEGACY"` resolve THROUGH the symlink to $UNIFIED, making
  # the "is unified root" guard below fire and silently mask the already-migrated case. So we
  # test the link on the RAW (un-canonicalized) path here, so the `already-symlinked` summary
  # counter is truthful on a 2nd `--apply`.
  if [ -L "$LEGACY" ]; then
    LEGACY_RAW="$LEGACY"
    if _is_symlink_to_unified "$LEGACY_RAW"; then
      log "ok    (already symlinked)      $LEGACY_RAW"; ALREADY=$((ALREADY + 1)); continue
    fi
    # A symlink to something ELSE: leave it, but warn (don't clobber a deliberate alias).
    err "skip  (symlink to non-unified target — left untouched) $LEGACY_RAW -> $(readlink "$LEGACY_RAW")"
    SKIPPED=$((SKIPPED + 1)); continue
  fi

  LEGACY_C="$(_canon "$LEGACY")"

  # Skip the unified root itself.
  if [ "$LEGACY_C" = "$UNIFIED" ]; then
    log "skip  (is unified root)        $LEGACY_C"; SKIPPED=$((SKIPPED + 1)); continue
  fi
  # Loop guard: skip any legacy root that lives INSIDE the unified root (e.g. the backup dir),
  # or that the unified root lives inside of.
  case "$LEGACY_C/" in "$UNIFIED"/*)
    log "skip  (inside unified root)    $LEGACY_C"; SKIPPED=$((SKIPPED + 1)); continue ;;
  esac
  case "$UNIFIED/" in "$LEGACY_C"/*)
    err  "skip  (unified is INSIDE this root — would loop) $LEGACY_C"; SKIPPED=$((SKIPPED + 1)); continue ;;
  esac

  log "MIGRATE                        $LEGACY_C"

  # 1. BACKUP -------------------------------------------------------------------------------
  # Sanitize the path for a filename. `/` and space both map to `_`, so the readable part is
  # NOT injective (e.g. `a/b c` and `a b/c` both -> `a_b_c`). Append a `cksum` of the FULL
  # path so distinct roots never collide and silently overwrite each other's rollback tarball.
  _san="$(printf '%s' "$LEGACY_C" | tr '/ ' '__' | sed 's/^_*//')"
  _san="${_san}-$(printf '%s' "$LEGACY_C" | cksum | cut -d' ' -f1)"
  _tarball="$BACKUP_DIR/${_san}.tar.gz"
  if [ "$APPLY" = "1" ]; then
    mkdir -p "$BACKUP_DIR"
    # tar the root by name from its parent so the archive is self-describing.
    ( cd "$(dirname "$LEGACY_C")" && tar -czf "$_tarball" "$(basename "$LEGACY_C")" )
    log "  backed up -> $_tarball"
  else
    log "  [dry-run] would back up -> $_tarball"
  fi

  # 2. MERGE per-agent *.md into unified ----------------------------------------------------
  if [ -d "$LEGACY_C" ]; then
    for AGENTDIR in "$LEGACY_C"/*/; do
      [ -d "$AGENTDIR" ] || continue
      _name="$(basename "$AGENTDIR")"
      for SRC in "$AGENTDIR"*.md; do
        [ -f "$SRC" ] || continue   # no *.md (or empty agent dir) -> nothing to merge
        _file="$(basename "$SRC")"
        _dest="$UNIFIED/$_name/$_file"
        if [ "$APPLY" = "1" ]; then
          _merge_files "$_dest" "$SRC"
          log "  merged $_name/$_file"
        else
          log "  [dry-run] would merge $_name/$_file -> $_dest"
        fi
      done
    done
  fi

  # 3. REPLACE legacy dir with a symlink to unified -----------------------------------------
  if [ "$APPLY" = "1" ]; then
    rm -rf "$LEGACY_C"
    ln -s "$UNIFIED" "$LEGACY_C"
    log "  symlinked $LEGACY_C -> $UNIFIED"
    printf '%s\n' "$LEGACY_C" >> "$PROCESSED_MARK"
  else
    log "  [dry-run] would symlink $LEGACY_C -> $UNIFIED"
  fi

  MIGRATED=$((MIGRATED + 1))
done < "$ROOTS_TMP"
rm -f "$ROOTS_TMP"

# ---- post-mutation inode verification (the symlink-farm invariant) ----------------------
if [ "$APPLY" = "1" ] && [ -s "$PROCESSED_MARK" ]; then
  log ""
  log "verifying inode-equality across the symlink-farm…"
  _bad=0
  while IFS= read -r LEGACY_C; do
    for UDIR in "$UNIFIED"/*/; do
      [ -d "$UDIR" ] || continue
      _name="$(basename "$UDIR")"
      # The migration replaces the legacy root's PARENT directory with a symlink to $UNIFIED,
      # so EVERY file under <legacy>/<agent>/ (lessons.md, notes.md, …) shares the unified
      # inode by construction — not just lessons.md. We verify lessons.md as a representative
      # sentinel; if its inode matches, all siblings under the same symlinked parent do too.
      _u="$UNIFIED/$_name/lessons.md"; _l="$LEGACY_C/$_name/lessons.md"
      [ -f "$_u" ] || continue
      if [ "$(_inode "$_u")" != "$(_inode "$_l")" ]; then
        err "INODE MISMATCH: $_l != $_u"; _bad=$((_bad + 1))
      fi
    done
  done < "$PROCESSED_MARK"
  if [ "$_bad" -ne 0 ]; then
    err "$_bad inode mismatch(es) — symlink-farm invariant violated"
    rm -f "$PROCESSED_MARK"; exit 1
  fi
  log "  OK — every migrated legacy root resolves to the unified inode"
fi
rm -f "$PROCESSED_MARK"

log ""
log "summary: discovered=$TOTAL migrated=$MIGRATED already-symlinked=$ALREADY skipped=$SKIPPED"
[ "$APPLY" = "1" ] || log "(dry-run — re-run with --apply to perform the migration)"
exit 0
