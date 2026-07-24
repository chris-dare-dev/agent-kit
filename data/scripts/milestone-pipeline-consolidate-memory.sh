#!/usr/bin/env bash
#
# milestone-pipeline-consolidate-memory.sh — keep .claude/agent-memory/<agent>/lessons*.md
# bounded so unbounded growth doesn't bloat the per-dispatch READ-FIRST cost.
#
# Usage:
#   milestone-pipeline-consolidate-memory.sh <agent-name>
#   milestone-pipeline-consolidate-memory.sh --all
#   milestone-pipeline-consolidate-memory.sh --status   # report sizes, no changes
#   milestone-pipeline-consolidate-memory.sh --extract-trajectories [--log <outcomes.jsonl>] [--dry-run]
#
# Triggered by:
#   - Manual invocation when an agent body warns it's about to exceed the 500-line cap.
#   - The milestone-pipeline slash command runs `--all` at orchestrator startup
#     so long-running pipelines don't surprise an agent mid-task.
#   - You can also wire this into a CI cron or a Claude Code hook (data/hooks/).
#
# What "consolidate" means:
#   - Read all lessons*.md files in the target agent's memory dir.
#   - Drop lines that are exact duplicates.
#   - Drop lines that are near-duplicates by trimmed-after-date prefix
#     (e.g. lessons of the form `<date> [<id>] <mode>: <text>` collapse when
#     two entries share the same <mode> + same normalized <text>; the older
#     date is dropped, keeping the most recent observation).
#   - Sort the survivors chronologically, write back atomically (.tmp + mv).
#
# What "--extract-trajectories" means (E3 / self-improving-tooling-m3, C3):
#   - Read the pipeline outcomes log (outcomes.jsonl) + derive cross-run calibration
#     signal via purely heuristic rules (RULE-1..5; see data/references/trajectory-extraction.md).
#   - Append at most one dated lesson line per fired rule to the relevant agent's
#     lessons.md ($AGENT_MEMORY_ROOT/<agent>/lessons.md), routed per rule:
#       RULE-1 (C+H concentration)  -> milestone-adversary
#       RULE-2 (rect_count uniform)  -> milestone-rectifier
#       RULE-3/4/5 (meta/advisory)   -> trajectory   (cross-pipeline accumulation dir)
#   - Every emitted lesson carries a `[n=<N>]` confidence tag; at a thin corpus
#     (<5 milestone records) RULE-5 fires a sparsity advisory so the signal is
#     never read as authoritative. Idempotent via exact-line dedupe.
#   - `--log <path>` overrides the outcomes.jsonl location; `--dry-run` prints the
#     would-be lessons and writes nothing. Does NOT mutate the dedupe/status paths.
#
# Safe to re-run; no-op if a file is already deduped and ≤ MAX_LINES, and
# idempotent for --extract-trajectories (no duplicate lesson lines on re-run).

set -euo pipefail

MAX_LINES="${MAX_LINES:-500}"

# Resolve the agent-memory base through the sanctioned unified-root helper so EVERY
# subcommand agrees on one base — the <agent>/--all/--status dedupe paths AND
# --extract-trajectories — honoring $AGENT_MEMORY_ROOT / $WORKSPACE_ROOT identically.
# (E3 M2 fix: previously the dedupe paths used a hardcoded relative `.claude/agent-memory`
# while the extractor resolved via agent_memory_base(); under either env var the two
# diverged and trajectory lessons were never trimmed by --all. Single source of truth now.)
_AMR_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
AMR_SOURCED_ONLY=1 . "$_AMR_SCRIPT_DIR/agent-memory-root.sh"
MEMORY_BASE="${MEMORY_BASE:-$(agent_memory_base)}"

# Lessons line format (per data/references/agent-memory.md):
#   YYYY-MM-DD [<id>] [<mode>:] <text>
# We extract the text-after-mode (or text-after-id if no mode) as the
# deduplication key — same observation made on different dates counts once.

dedupe_one_file() {
  local file="$1"
  [ -f "$file" ] || return 0

  python3 - "$file" "$MAX_LINES" <<'PY'
import re, sys, pathlib, datetime

path, max_lines = sys.argv[1], int(sys.argv[2])
p = pathlib.Path(path)
lines = [l.rstrip("\n") for l in p.read_text().splitlines() if l.strip()]

# Parse: <date> [<id>] [<mode>:] <text>
# Key the dedupe on (mode, normalized text). Keep the most-recent date per key.
LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+\[(?P<id>[^\]]+)\]\s+"
    r"(?:(?P<mode>[A-Za-z0-9_\-]+):\s+)?"
    r"(?P<text>.+)$"
)

def normalize(text):
    # Lowercase, collapse whitespace, strip trailing punctuation — coarse but effective.
    s = " ".join(text.lower().split())
    return s.rstrip(" .;,:")

best = {}  # (mode, normalized_text) -> (date_str, full_line)
unparsable = []
for line in lines:
    m = LINE_RE.match(line)
    if not m:
        unparsable.append(line)
        continue
    key = (m.group("mode") or "", normalize(m.group("text")))
    prior = best.get(key)
    if prior is None or m.group("date") > prior[0]:
        best[key] = (m.group("date"), line)

# Emit unique lines, sorted by date ascending, then the unparsable (unchanged tail).
out = [v[1] for v in sorted(best.values(), key=lambda x: x[0])]
out.extend(unparsable)

# If even after dedupe we exceed MAX_LINES, drop oldest entries.
if len(out) > max_lines:
    overflow = len(out) - max_lines
    out = out[overflow:]
    out.insert(0, f"# Consolidated {datetime.date.today().isoformat()}: dropped {overflow} oldest entries to enforce max-lines={max_lines}")

if out != lines:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.replace(p)
    print(f"  REWROTE {p} ({len(lines)} -> {len(out)} lines)")
else:
    print(f"  CLEAN   {p} ({len(lines)} lines)")
PY
}

agent_status() {
  local agent="$1"
  local agent_dir="${MEMORY_BASE}/${agent}"
  [ -d "$agent_dir" ] || { echo "  (no memory dir yet for ${agent})"; return 0; }

  echo "  ${agent}:"
  find "$agent_dir" -maxdepth 1 -type f -name 'lessons*.md' -print0 \
    | while IFS= read -r -d '' f; do
        local lc; lc=$(wc -l < "$f" | tr -d ' ')
        local flag=""
        if [ "$lc" -gt "$MAX_LINES" ]; then flag=" ⚠ exceeds ${MAX_LINES}"; fi
        printf "    %-60s %5s lines%s\n" "$(basename "$f")" "$lc" "$flag"
      done
}

agent_dedupe() {
  local agent="$1"
  local agent_dir="${MEMORY_BASE}/${agent}"
  [ -d "$agent_dir" ] || { echo "  (no memory dir for ${agent}, nothing to do)"; return 0; }

  echo "Consolidating ${agent}:"
  find "$agent_dir" -maxdepth 1 -type f -name 'lessons*.md' -print0 \
    | while IFS= read -r -d '' f; do
        dedupe_one_file "$f"
      done
}

# --extract-trajectories: derive cross-run calibration lessons from the outcomes
# log and append them (routed per rule) to the relevant agent lessons.md files.
# Additive; never touches the dedupe/status code paths. Args (any order after the
# subcommand): --log <outcomes.jsonl>  --dry-run
extract_trajectories() {
  local outcomes_log="" dry_run="0"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --log) outcomes_log="${2:-}"; shift 2 ;;
      --dry-run) dry_run="1"; shift ;;
      *) echo "extract_trajectories: unknown arg '$1'" >&2; return 2 ;;
    esac
  done

  # Default outcomes.jsonl resolution mirrors pipeline-outcome-log.py.
  if [ -z "$outcomes_log" ]; then
    local toplevel
    toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$toplevel" ]; then
      outcomes_log="$toplevel/.claude/notes/pipeline-outcomes/outcomes.jsonl"
    else
      outcomes_log=".claude/notes/pipeline-outcomes/outcomes.jsonl"
    fi
  fi

  # Use the SAME resolved base as every other subcommand (sourced once at top — E3 M2 fix:
  # extraction and consolidation must agree so trajectory lessons are dedupe/trim-eligible).
  local amr_base="$MEMORY_BASE"

  if [ ! -f "$outcomes_log" ]; then
    echo "(no outcomes log at $outcomes_log; nothing to extract)"
    return 0
  fi

  echo "Extracting trajectory lessons from $outcomes_log (base=$amr_base, dry_run=$dry_run):"
  python3 - "$outcomes_log" "$amr_base" "$dry_run" <<'PY'
import json, sys, pathlib, datetime

log_path = pathlib.Path(sys.argv[1])
amr_base = pathlib.Path(sys.argv[2])
dry_run = sys.argv[3] == "1"
today = datetime.date.today().isoformat()
MID = "self-improving-tooling-m3"


def load_records(path):
    """Tolerant JSONL parse; mirrors pipeline-outcome-log.read_records."""
    records = []
    if not path.exists():
        return records
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def num(v):
    """Coerce a severity column to int, or None if null/absent/non-numeric."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ch_mass(r):
    c = num(r.get("critique_critical"))
    h = num(r.get("critique_high"))
    if c is None or h is None:
        return None
    return c + h


records = load_records(log_path)

# Milestone records WITH full severity signal (all four C/H/M/L columns non-null).
milestone_records = [
    r for r in records
    if r.get("pipeline") == "milestone"
    and all(num(r.get(col)) is not None
            for col in ("critique_critical", "critique_high",
                        "critique_medium", "critique_low"))
]
spike_records = [r for r in records if r.get("pipeline") == "spike"]

# (agent_dir, line) tuples to emit. Each line follows the canonical format
#   YYYY-MM-DD [<id>] <mode>: <text>
# with the n-tag embedded in the mode payload (Option A) so dedupe_one_file's
# LINE_RE captures mode=trajectory and the n-tag is part of the dedupe key.
emissions = []


def lesson(agent, text):
    emissions.append((agent, "%s [%s] trajectory: %s" % (today, MID, text)))


n_ms = len(milestone_records)

# RULE-5 (sparsity advisory) — evaluated first, fires alongside others.
if n_ms < 5:
    lesson(
        "trajectory",
        "[n=%d] milestone corpus is sparse (%d records with severity signal); "
        "all trajectory lessons in this run carry low confidence — re-run "
        "--extract-trajectories after accruing >=5 milestone records for stronger "
        "calibration signal" % (n_ms, n_ms),
    )

# RULE-1: C+H mass concentration -> milestone-adversary.
if n_ms >= 3:
    masses = [(r, ch_mass(r)) for r in milestone_records]
    masses = [(r, m) for (r, m) in masses if m is not None]
    total_ch = sum(m for (_, m) in masses)
    if total_ch > 0:
        top_r, top_m = max(masses, key=lambda x: x[1])
        if top_m > total_ch / 2:
            lesson(
                "milestone-adversary",
                "[n=%d] C+H findings are heavily concentrated in a single milestone "
                "(%s, C+H=%d of %d); at n=%d this is low-confidence signal — adversary "
                "should NOT lower CRITICAL/HIGH bars, only audit fuzzy-axis HIGH triggers "
                "for false-positive patterns"
                % (n_ms, top_r.get("id"), top_m, total_ch, n_ms),
            )

# RULE-2: rect_count uniform despite severity variance -> milestone-rectifier.
if n_ms >= 3:
    rects = [num(r.get("rectification_count")) for r in milestone_records]
    rects = [v for v in rects if v is not None]
    masses = [m for m in (ch_mass(r) for r in milestone_records) if m is not None]
    if len(rects) == n_ms and len(set(rects)) == 1 and masses and max(masses) > 0:
        lesson(
            "milestone-rectifier",
            "[n=%d] rect_count was uniform (%d) across all %d milestone records despite "
            "C+H variance (range %d-%d); at n=%d this suggests MEDIUM findings drove "
            "rectification even in the zero-C+H runs — verify MEDIUM findings are not "
            "suppressed relative to HIGH"
            % (n_ms, rects[0], n_ms, min(masses), max(masses), n_ms),
        )

# RULE-3: spike NO verdict -> trajectory.
if len(spike_records) >= 2:
    no_spikes = [r for r in spike_records if r.get("verdict") == "NO"]
    if no_spikes:
        ids = ", ".join(str(r.get("id")) for r in no_spikes)
        lesson(
            "trajectory",
            "[n=%d] %d of %d spikes returned NO verdict (ids: %s); spike_review_verdict "
            "was ACCEPT in all cases — NO verdict + ACCEPT review is a valid "
            "'hypothesis disproved cleanly' path, NOT a failure signal"
            % (len(spike_records), len(no_spikes), len(spike_records), ids),
        )

# RULE-4: token_cost null advisory -> trajectory.
if records and all(r.get("token_cost") is None for r in records):
    lesson(
        "trajectory",
        "[n=%d] token_cost is null in all %d records — token calibration KR cannot be "
        "evaluated until --field token_cost= wiring lands; E4 OTel spike is the "
        "prerequisite" % (len(records), len(records)),
    )


def already_written(lessons_path, line):
    if not lessons_path.exists():
        return False
    existing = {l.strip() for l in lessons_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    return line.strip() in existing


appended = skipped = 0
for agent, line in emissions:
    agent_dir = amr_base / agent
    lessons_path = agent_dir / "lessons.md"
    if already_written(lessons_path, line):
        skipped += 1
        print("  SKIP (exists) %s" % agent)
        continue
    if dry_run:
        print("  [DRY-RUN] WOULD APPEND to %s:" % lessons_path)
        print("    %s" % line)
        continue
    agent_dir.mkdir(parents=True, exist_ok=True)
    with open(str(lessons_path), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    appended += 1
    print("  APPEND -> %s" % lessons_path)

if dry_run:
    print("  (dry-run: %d candidate lessons, %d already present)" % (len(emissions) - skipped, skipped))
else:
    print("  (appended %d, skipped %d already-present)" % (appended, skipped))
PY
}

main() {
  case "${1:-}" in
    "")
      echo "usage: $(basename "$0") <agent-name> | --all | --status | --extract-trajectories [--log <path>] [--dry-run]" >&2
      exit 2
      ;;
    --extract-trajectories)
      shift
      extract_trajectories "$@"
      ;;
    --status)
      echo "Lessons file sizes (cap=${MAX_LINES}):"
      if [ -d "$MEMORY_BASE" ]; then
        for d in "$MEMORY_BASE"/*/; do
          [ -d "$d" ] || continue
          agent_status "$(basename "$d")"
        done
      else
        echo "  (no ${MEMORY_BASE} dir)"
      fi
      ;;
    --all)
      if [ -d "$MEMORY_BASE" ]; then
        for d in "$MEMORY_BASE"/*/; do
          [ -d "$d" ] || continue
          agent_dedupe "$(basename "$d")"
        done
      else
        echo "  (no ${MEMORY_BASE} dir)"
      fi
      ;;
    *)
      agent_dedupe "$1"
      ;;
  esac
}

main "$@"
