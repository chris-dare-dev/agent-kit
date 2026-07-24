#!/usr/bin/env bash
# Initialize milestone delivery-state v2 directory and state.json.
# Usage: init-state.sh <milestone-id> [--brief "verbatim user brief"] [--repo-root PATH]
#                      [--override "reason"]
#
# Idempotent for schema v2: if state.json exists, prints current phase and exits
# 0 without modifying anything. A v1/unversioned state refuses with exit 5 and
# must go through milestone-pipeline-migrate.py explicitly.
#
# DEPENDENCY GATE: if the id belongs to a roadmap milestone register
# (.claude/notes/roadmaps/<slug>/milestones.json — schema:
# data/references/roadmap-milestones-schema.md), a FRESH init refuses (exit 3)
# while any depends_on milestone is not 'complete'. Pass --override "reason" to
# proceed anyway — audited into the register, never silent. Ad-hoc ids (GitLab
# iids, W-numbers) that appear in no register skip the gate.
#
# MULTI-REPO REFUSAL: a FRESH init refuses (exit 6, NO override) when the
# register declares more than one `repos` entry for the id. delivery-state v2
# delivers exactly ONE source repository per milestone; split multi-repo work
# into one milestone per repo chained with depends_on. See
# data/commands/milestone-pipeline.md § Multi-repo.
#
# Two-phase to stay consistent under failure: (1) --check-only dry-run of the
# gate BEFORE any state is created, (2) create state.json, (3) the real register
# write LAST — so a refused init leaves nothing behind, and a mid-flight failure
# leaves the register still 'pending' (reconcile flags it) rather than
# 'in_progress' with a dangling state_path.
#
# Repo-root detection (in order):
#   1. --repo-root flag if passed
#   2. $REPO_ROOT env var if set
#   3. `git rev-parse --show-toplevel` from CWD if currently inside a git repo
#   4. Fallback: walk up from this script's dir to nearest .git/

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: init-state.sh <milestone-id> [--brief \"...\"] [--repo-root PATH] [--override \"reason\"]" >&2
  exit 2
fi

ID="$1"
shift

# Milestone-id containment (round-4 F10): the id becomes a path segment under
# .claude/notes/milestones/ — separators or a leading dot would escape the state
# dir ('../evil'). Same rule in checkpoint.py and findings.py; keep in sync.
case "$ID" in
  */*|*\\*|.*|"")
    echo "invalid milestone id '$ID' — ids are [A-Za-z0-9][A-Za-z0-9._-]* (no path separators)" >&2
    exit 2
    ;;
esac
if ! printf '%s' "$ID" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
  echo "invalid milestone id '$ID' — ids are [A-Za-z0-9][A-Za-z0-9._-]* (no path separators)" >&2
  exit 2
fi

BRIEF=""
REPO_ROOT_OVERRIDE=""
DEP_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --brief)
      BRIEF="${2:-}"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT_OVERRIDE="${2:-}"
      shift 2
      ;;
    --override)
      DEP_OVERRIDE="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# Repo-root detection.
#
# Resolution order (FIRST match wins):
#   1. --repo-root flag
#   2. $REPO_ROOT env var
#   3. $PLATFORM_ROOT env var (set by the MCP server — the milestone's home
#      cluster repo, NOT the MCP server's own repo)
#   4. `git rev-parse --show-toplevel` from CWD
#   5. Last-resort: walk up from $0 — this is dangerous when this script
#      is symlinked into a sandbox or worktree because realpath could
#      land in the MCP server repo instead of the platform repo. Step 5
#      is intentionally last and emits a warning when it fires.
if [[ -n "$REPO_ROOT_OVERRIDE" ]]; then
  REPO_ROOT="$REPO_ROOT_OVERRIDE"
elif [[ -n "${REPO_ROOT:-}" ]]; then
  : # use env var as-is
elif [[ -n "${PLATFORM_ROOT:-}" && -d "$PLATFORM_ROOT" ]]; then
  REPO_ROOT="$PLATFORM_ROOT"
elif REPO_ROOT_FROM_CWD=$(git rev-parse --show-toplevel 2>/dev/null); then
  REPO_ROOT="$REPO_ROOT_FROM_CWD"
else
  REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$REPO_ROOT" ]]; then
    echo "WARN: repo root resolved via script location ($REPO_ROOT) — this may not match the milestone's home cluster repo if this script is symlinked from a sandbox/worktree. Prefer setting REPO_ROOT or PLATFORM_ROOT env var." >&2
  fi
fi

if [[ -z "$REPO_ROOT" || ! -d "$REPO_ROOT" ]]; then
  echo "could not determine repo root. Pass --repo-root PATH or set REPO_ROOT/PLATFORM_ROOT env var." >&2
  exit 2
fi

# DEPLOY-REPO REFUSAL (round-4 F6). deploy/argocd-config-* repos are
# CI-generated; milestones NEVER run there — the fix belongs in the source
# repo (charts/, infra/, source/). The command's old `^deploy/argocd-config-`
# diff-path guard could not fire inside a deploy-repo CLONE (the platform is
# independent clones; paths there are repo-relative), so the refusal is by
# REPO IDENTITY (directory basename or origin URL), enforced deterministically
# at init rather than as command prose.
REPO_BASENAME="$(basename "$REPO_ROOT")"
ORIGIN_URL="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
if [[ "$REPO_BASENAME" == argocd-config-* || "$ORIGIN_URL" == *argocd-config-* ]]; then
  echo "init refused: '$REPO_BASENAME' is a deploy repo (argocd-config-* is CI-generated)." >&2
  echo "Run the milestone in the SOURCE repo — CI re-renders deploy/ and silently undoes direct edits." >&2
  exit 2
fi

DIR="$REPO_ROOT/.claude/notes/milestones/$ID"
STATE="$DIR/state.json"

if [[ -f "$STATE" ]]; then
  STATE_META=$(python3 - "$STATE" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(f"{s.get('schema_version', 'v1-unversioned')}\t{s.get('phase', 'unknown')}")
PY
)
  VERSION="${STATE_META%%$'\t'*}"
  PHASE="${STATE_META#*$'\t'}"
  if [[ "$VERSION" != "2" ]]; then
    echo "state exists at $STATE but is schema $VERSION (phase=$PHASE)." >&2
    echo "Run: python3 $(dirname "$0")/milestone-pipeline-migrate.py '$ID' --repo-root '$REPO_ROOT'" >&2
    echo "Implicit/mixed migration is forbidden because v1 complete does not prove live operation." >&2
    exit 5
  fi
  echo "state already exists at $STATE (phase=$PHASE) — resuming"
  exit 0
fi

# ---------------------------------------------------------------------------
# DEPENDENCY GATE, phase 1: dry-run BEFORE creating anything (fresh init only —
# the resume path above never re-gates). The real register write happens LAST.
# ---------------------------------------------------------------------------
STATUS_SCRIPT="$(dirname "$0")/roadmap-milestones-status.py"
REGISTER=""
if [[ -f "$STATUS_SCRIPT" ]]; then
  FIND_RC=0
  REGISTER="$(python3 "$STATUS_SCRIPT" --find "$REPO_ROOT" "$ID")" || FIND_RC=$?
  if [[ $FIND_RC -eq 2 ]]; then
    echo "init refused: milestone id '$ID' is claimed by MULTIPLE registers (see above)." >&2
    echo "Fix the duplicate id before running the pipeline." >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# MULTI-REPO REFUSAL (fresh init only; the resume path above never reaches here)
# ---------------------------------------------------------------------------
# delivery-state v2 delivers exactly ONE source repo per milestone
# (artifacts.py:1661). That refusal is correct and stays — but it fires at
# code-complete, after research + implementation + critique are all sunk, and by
# then the state is UNRECOVERABLE: implementation_commits is writable only in
# implement-running (checkpoint.py FIELD_WRITABLE_PHASES) and PHASE_EDGES has no
# backward edge, so a multi-repo milestone past implement-complete can only be
# DISCARDED, never repaired. dispatcher-receipt-authz-m3 is the worked example
# (2026-07-16): two repos declared in its register, wedged at critique-running,
# abandoned.
#
# SWEEPS THE WHOLE PLATFORM TREE, not just $REPO_ROOT. `--find` searches only
# <REPO_ROOT>/.claude/notes/roadmaps/, so an earlier revision of this gate — which
# lived inside `if [[ -n "$REGISTER" ]]` — was blind to a milestone whose register
# lives in a SIBLING clone. Empirically probed: a 2-repo entry registered in repo
# A, init run from declared target repo B, returned rc=0 and created state. Four
# of the EIGHT queued multi-repo milestones (dna-rem-m7,
# svcreg-per-user-homescreen-m3, session-logout-idle-timeout-m2,
# svcreg-kargo-prod-m2) declare NO repo that hosts their own register, so the old
# shape could never fire for them. The sweep closes that.
#
# The figure was "six" until 2026-07-17: that count was itself produced by the
# depth-2 assumption this gate has since dropped (V-H1), which hid the two
# milestones whose registers sit at the platform root and the workspace root.
# Recount with the ancestor walk below, not with a fixed-depth glob.
#
# Deliberately scoped to THIS gate. The dependency gate above still resolves only
# the local $REGISTER; its identical cross-clone blindness is DOCUMENTED in
# `data/commands/milestone-pipeline.md` § Multi-repo rather than silently changed
# here, because widening it would alter depends_on behavior platform-wide — a
# separate, unscoped change.
#
# No --override: an unconditional refusal like the deploy-repo check (exit 2), not
# a "not yet ready" gate (exit 3). Runs BEFORE the dependency gate's verdict is
# acted on and before mkdir, so a refusal leaves nothing behind. Fail-open on an
# unparseable register is INHERITED, not new: roadmap-milestones-status.py
# find_file() (:151-176) already degrades an unreadable register to ad-hoc
# behavior identically; artifacts.py:1661 remains the backstop.
REPOS_N=$(python3 - "$ID" "$REGISTER" "$REPO_ROOT" "${PERSONAL_WORKSPACE_ROOT:-}" <<'PY' 2>/dev/null
import glob, json, os, sys
mid, local_reg, repo_root, ws_env = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

# V-H1 (m1dia04/m1dia05): the first sweep anchored at a fixed "$REPO_ROOT/../.."
# and globbed exactly "*/*/", encoding depth-2 in TWO places. It was therefore
# inert for the four depth-1 clones (ci-cd-templates, plans, platform-model,
# sandbox) and for any register at the platform root or the workspace root.
# Measured 2026-07-17 against the live tree: 2 of the 8 pending multi-repo
# milestones bypassed it entirely -- session-logout-idle-timeout-m2 (3 repos,
# register at the platform root) and svcreg-kargo-prod-m2 (2 repos, register at
# the workspace root). Neither declares a repo hosting its own register, so
# `--find` misses them too and the whole gate no-opped: init returned rc=0 and
# created state. 537c5d4's own "six queued" comment was an undercount produced
# by this same blindness -- the true figure is 8.
#
# Walk ANCESTORS instead of assuming a depth: a clone's platform root and
# workspace root are both ancestors of it whatever its own depth, so globbing
# 0-2 levels below each ancestor covers every real layout AND the depth-2
# sibling shape the original probe pins. Deliberately marker-free (NOT the
# "walk up for charts/" idiom of keycloak-live-drift.py:96) so the gate does not
# depend on platform directory names and the self-test fixtures need no
# synthetic charts/ tree. Bounded at 6 ancestors: from the deepest real clone
# (platform/tools/claude-mcp-server) that reaches the workspace root and stops.
roots, cur = [], os.path.abspath(repo_root)
for _ in range(6):
    roots.append(cur)
    parent = os.path.dirname(cur)
    if parent == cur:
        break
    cur = parent
if ws_env:
    roots.append(os.path.abspath(ws_env))

paths = [local_reg] if local_reg else []
for r in roots:
    for depth in ("", "*/", "*/*/"):
        paths += glob.glob(os.path.join(r, depth + ".claude/notes/roadmaps/*/milestones.json"))

best, where = 0, ""
for p in sorted(set(paths)):
    try:
        for m in json.load(open(p)).get("milestones", []):
            if m.get("id") == mid and len(m.get("repos") or []) > best:
                best, where = len(m["repos"]), p
    except Exception:
        continue
print(f"{best}\t{where}")
PY
)
REPOS_WHERE="${REPOS_N#*$'\t'}"
REPOS_N="${REPOS_N%%$'\t'*}"
[[ "$REPOS_N" =~ ^[0-9]+$ ]] || REPOS_N=0
if (( REPOS_N > 1 )); then
  echo "init refused: '$ID' declares $REPOS_N repos in the register; a milestone delivers to exactly ONE." >&2
  echo "  register: ${REPOS_WHERE:-<not found>}" >&2
  echo "Two repos cannot be pushed atomically, so a multi-repo milestone can half-land and its" >&2
  echo "closure receipt would attest a delivery that only partially happened." >&2
  echo "Split it: one milestone per repo, chained with depends_on (the dependency ships first)." >&2
  echo "See: data/commands/milestone-pipeline.md § Multi-repo" >&2
  exit 6
fi

if [[ -f "$STATUS_SCRIPT" ]]; then
  if [[ -n "$REGISTER" ]]; then
    GATE_ARGS=("$REGISTER" "$ID" "in_progress" --check-only)
    [[ -n "$DEP_OVERRIDE" ]] && GATE_ARGS+=(--override "$DEP_OVERRIDE")
    GATE_RC=0
    python3 "$STATUS_SCRIPT" "${GATE_ARGS[@]}" || GATE_RC=$?
    if [[ $GATE_RC -eq 3 ]]; then
      echo "init refused by dependency gate (register: $REGISTER)." >&2
      echo "Re-run with: init-state.sh $ID --override \"<reason>\" ... to proceed anyway (audited)." >&2
      exit 3
    elif [[ $GATE_RC -ne 0 ]]; then
      echo "init refused: register transition would be illegal (register: $REGISTER)." >&2
      echo "A 'complete' milestone is terminal — a genuine re-run is a NEW milestone id." >&2
      exit "$GATE_RC"
    fi
  fi
fi

mkdir -p "$DIR/research" "$DIR/artifacts"

# Ensure Claude pipeline artifacts are excluded in the target repo (idempotent,
# non-fatal; .git/info/exclude — never mutates the tracked .gitignore).
"$(dirname "$0")/ensure-claude-gitignore.sh" "$REPO_ROOT" 2>/dev/null || true

NOW=$(python3 -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
AGENT_KIT_ROOT=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parents[2])' "$0")
AGENT_KIT_COMMIT=$(git -C "$AGENT_KIT_ROOT" rev-parse --verify HEAD)
KIT_STATUS=$(git -C "$AGENT_KIT_ROOT" status --porcelain --untracked-files=all -- \
  data/agents \
  data/commands/milestone-pipeline.md \
  data/provider-adapters/codex/entrypoints/milestone-pipeline \
  data/references/milestone-pipeline-* \
  data/references/pipeline-pattern-v2.md \
  data/schemas \
  data/scripts/milestone-pipeline-* \
  data/scripts/milestone-render-provenance.py \
  data/model-policy.json \
  data/facts/catalog.json)
if [[ -n "$KIT_STATUS" ]]; then
  echo "init refused: canonical pipeline kit has tracked or untracked changes; commit/regenerate it first." >&2
  echo "$KIT_STATUS" >&2
  exit 2
fi

python3 - "$STATE" "$ID" "$NOW" "$BRIEF" "$AGENT_KIT_COMMIT" <<'PY'
import json, sys, os
state_path, mid, now, brief, agent_kit_commit = sys.argv[1:6]
state = {
    "schema_version": 2,
    "id": mid,
    "created_at": now,
    "updated_at": now,
    "phase": "init",
    "phase_history": [{"phase": "init", "at": now}],
    "agent_kit_commit": agent_kit_commit,
    "kit_upgrade_history": [],
    "check_run_head": None,
    "check_run_hashes": {},
    "check_run_history": {},
    "check_run_attempts": [],
    "milestone_brief": brief,
    "research_mode": None,
    "research_briefs": [],
    "research_synthesis": None,
    "implementation_path": None,
    "implementation_specialist": None,
    "implementation_base": None,
    "implementation_commit_range": None,
    "implementation_commits": [],
    "implementation_branch": None,
    "critique_path": None,
    "critics_run": [],
    "critique_files": [],
    "critique_finding_counts": None,
    "findings_register": None,
    "rectification_commit": None,
    "rectification_not_required_reason": None,
    "fixed_findings": [],
    "deferred_findings": [],
    "invalidated_findings": [],
    "regression_tests_added": [],
    "publication_required": True,
    "publication_not_required_reason": None,
    "operations_required": True,
    "operations_not_required_reason": None,
    "implementation_status": "pending",
    "operational_status": "pending",
    "review_status": "pending",
    "review_manifest": "artifacts/review-manifest.json",
    "implementation_evidence": "artifacts/implementation-evidence.json",
    "publication_intent": "artifacts/publication-intent.json",
    "release_manifest": "artifacts/release-manifest.json",
    "operations_plan": "artifacts/operations-plan.json",
    "operations_evidence": "artifacts/operations-evidence.json",
    "waivers": "artifacts/waivers.json",
    "artifact_bindings": {},
    "migration": None,
}
tmp = state_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
os.replace(tmp, state_path)
PY

# ---------------------------------------------------------------------------
# DEPENDENCY GATE, phase 2: the real register write — LAST, after state.json
# exists, so failure ordering never leaves 'in_progress' with a dangling
# state_path. If this write fails (e.g. a concurrent session raced the gate),
# state.json exists while the register stays 'pending' — reconcile-visible;
# resolve with the printed command.
# ---------------------------------------------------------------------------
if [[ -n "$REGISTER" ]]; then
  WRITE_ARGS=("$REGISTER" "$ID" "in_progress" --field "run.state_path=.claude/notes/milestones/$ID/state.json")
  [[ -n "$DEP_OVERRIDE" ]] && WRITE_ARGS+=(--override "$DEP_OVERRIDE")
  if ! python3 "$STATUS_SCRIPT" "${WRITE_ARGS[@]}"; then
    echo "WARN: state.json created but the register write failed (raced by a concurrent session?)." >&2
    echo "Resolve manually: python3 $STATUS_SCRIPT $REGISTER $ID in_progress --field run.state_path=.claude/notes/milestones/$ID/state.json" >&2
    exit 4
  fi
  echo "  register:  $REGISTER (marked in_progress)"
fi

echo "initialized $STATE"
echo "  repo_root: $REPO_ROOT"
echo "  brief:     $(if [[ -n "$BRIEF" ]]; then echo "set ($(echo -n "$BRIEF" | wc -c | tr -d ' ') chars)"; else echo "(empty — pass --brief to populate)"; fi)"
echo "  phase:     init"
