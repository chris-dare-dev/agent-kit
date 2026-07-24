---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Self-improvement operations — the learning-loop runbook (who runs what, when)

**Scope:** operating the library's self-improvement loop. The loop is fully built and wired; what it
needs is an OPERATOR on a cadence, not an expert. Any engineer (or a Sonnet-class agent session,
with the one gated step called out below) can run everything here. Component deep-dives live in
`pipeline-outcome-log-schema.md`, `trajectory-extraction.md`, `sealed-eval.md`,
`agent-memory.md`, and `agent-memory-consolidation.md` — this file is the operations layer on top.

**When NOT to use this file:** you want to understand or edit agent memory content →
`agent-memory.md`; you want the outcome-record schema → `pipeline-outcome-log-schema.md`.

## 1. The loop in one diagram

```
pipeline runs (milestone/spike/argoops/…)
      │  emit (automatic, wired into every pipeline terminal)
      ▼
outcomes.jsonl            ← machine-local, gitignored (one JSONL record per run)
      │  --extract-trajectories (MANUAL, on the cadence below)
      ▼
agent lessons.md files    ← machine-local calibration signal ([n=N] confidence tags)
      │  re-calibrate (MANUAL edit, protocol in §4)
      ▼
"Data-calibrated guidance" sections in pipeline agent bodies (data/agents/*, committed)
      │  sealed-eval metric (MANUAL, per thresholds)
      ▼
delta vs the sealed baseline → dated addendum in plans/self-improving-tooling-m3-baseline.md
```

Capture is automatic; **learning and evaluation are manual and die without a cadence.** That is the
historical failure mode: extraction ran once 2026-06-18 at n=4, then nothing until 2026-07-02 at
n=43, despite every re-run threshold being crossed.

## 2. Environment contract (read runtime-contract.md first)

All commands below run **from the claude-mcp-server repo root** and resolve the workspace as:

```bash
WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
cd "$WS/GitLab/workspace/platform/tools/claude-mcp-server"
export WORKSPACE_ROOT="$WS"   # makes the agent-memory scripts resolve the unified root
```

`.py` scripts run with `python3`, `.sh` with `bash` — never `bash <x>.py` (runtime-contract.md §3).

## 3. Where the data lives (and what survives a laptop)

| Artifact | Path | Git status |
|---|---|---|
| Live outcome log | `$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl` | machine-local, NEVER committed (by design — see pipeline-outcome-log-schema.md) |
| Committed corpus snapshots | `data/scripts/sealed-eval-v0-corpus.jsonl` (9 records, sha256-sealed via `sealed-eval-v0-manifest.json` — never modify) and `data/scripts/sealed-eval-v1-corpus.jsonl` (43 records: 26 milestone + 17 spike, snapshotted 2026-07-02, unsealed working snapshot) | committed |
| Agent lessons (calibration memory) | `$WS/.claude/agent-memory/<agent>/lessons.md` (unified root; all repo-level `.claude/agent-memory` paths are symlinks to it) | machine-local, gitignored |
| Calibration that DOES survive | the "Data-calibrated guidance" section in `data/agents/milestone-adversary.md` (and any sibling that gains one) + dated addenda in `plans/self-improving-tooling-m3-baseline.md` | committed |

**Fresh-machine seed policy:** a new checkout starts with an EMPTY outcome log and empty agent
memory — that is expected. Seed the corpus from the committed snapshot so cadence math and metrics
have history:

```bash
mkdir -p "$WS/.claude/notes/pipeline-outcomes"
cp data/scripts/sealed-eval-v1-corpus.jsonl "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl"
```

New runs append on top (records are self-describing JSONL; mixing snapshot + local records is fine).
Do NOT seed from `sealed-eval-v0-corpus.jsonl` — that file is the sealed baseline and must stay
byte-identical to its manifest.

## 4. The cadence (calendar this — nothing re-runs itself)

| Step | Trigger | Owner |
|---|---|---|
| A. Trajectory extraction (§5) | every **+10 outcome records** since the last run, or **monthly**, whichever first | any engineer |
| B. Sealed-eval re-measure + addendum (§6) | with every step A, per sealed-eval.md §8 (thresholds ≥5 / ≥10 long since crossed — so: same cadence as A) | any engineer |
| C. Agent-body re-calibration (§7) | when step A/B shows a stale claim in an agent body's data-calibrated section | any engineer (commit + push = gated external write) |
| D. Health checks (§8) | quarterly, and on any new machine | any engineer |

Check the record count any time:

```bash
python3 data/scripts/pipeline-outcome-log.py summary --pipeline milestone --json \
  --log "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'
```

**Where to find the last run's n (to compute the "+10 since the last run" trigger):** the
`(n=…, re-calibrated YYYY-MM-DD)` header of the "Data-calibrated guidance" section in
`data/agents/milestone-adversary.md`, and the newest dated addendum in
`plans/self-improving-tooling-m3-baseline.md` — both are updated by step C, so they always
carry the n of the last completed extraction.

## 5. Step A — trajectory extraction (exact commands)

```bash
# dry-run first — prints the would-be lessons, writes nothing:
bash data/scripts/milestone-pipeline-consolidate-memory.sh --extract-trajectories \
  --log "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl" --dry-run
# then apply:
bash data/scripts/milestone-pipeline-consolidate-memory.sh --extract-trajectories \
  --log "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl"
```

Pass `--log` explicitly — the default resolves via `git rev-parse --show-toplevel`, which lands on
whatever repo you happen to be standing in (and FAILS at the workspace/platform levels, which are
not git repos — see git-topology.md). Idempotent: re-running appends nothing new. Rules RULE-1..5
and routing are documented in trajectory-extraction.md; every emitted lesson carries an `[n=N]`
confidence tag. Expect rules to stop firing as the corpus matures (at n=26/2026-07-02, only the
spike-NO advisory fired — that is the heuristics self-correcting, not a failure).

## 6. Step B — sealed-eval re-measure (exact commands)

```bash
python3 data/scripts/sealed-eval.py verify data/scripts/sealed-eval-v0-manifest.json
# tamper-check the v0 seal first; must print "verify: MATCH sha256=…" and exit 0
python3 data/scripts/sealed-eval.py metric \
  --corpus "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl" \
  --baseline data/scripts/sealed-eval-v0-manifest.json
```

Read `overclaim_guard_fired`: if `True` (n_post < 5) the delta is NOT claimable. Append a dated
addendum with the numbers to `plans/self-improving-tooling-m3-baseline.md` (format precedent: the
2026-07-02 addendum there — reference run: `post_ch_per_milestone=0.46`, `n_post=26`,
`delta=-0.54`, guard not fired). Never mutate the sealed v0 corpus/manifest; new snapshots get new
version numbers (`sealed-eval-v2-corpus.jsonl`, …) and old ones are kept.

Also refresh the committed working snapshot at each step B so fresh machines seed near-current:

```bash
cp "$WS/.claude/notes/pipeline-outcomes/outcomes.jsonl" data/scripts/sealed-eval-v1-corpus.jsonl
# (bump to -v2 etc. if the owner wants history preserved; note the date in the addendum)
```

## 7. Step C — re-calibrating agent bodies

The one committed consumer today is `data/agents/milestone-adversary.md` → "Data-calibrated
guidance" (re-calibrated to n=26 on 2026-07-02). Protocol (embedded in that section itself):
run the §4 summary, append the §6 addendum FIRST, then edit the section's claims to match the new
numbers, updating its `(n=…, re-calibrated YYYY-MM-DD)` header. The absolute-rule CRITICAL bar in
that section is IMMUTABLE — calibration only ever touches the fuzzy-axis guidance. Committing +
pushing `data/` is an external write: commit locally, then request push authorization
(workspace CLAUDE.md, "Sharing changes").

## 8. Step D — health checks (quarterly + every new machine)

**Agent-memory symlink integrity** — every repo-level `.claude/agent-memory` must be a symlink to
the unified root (a plain FILE or a real directory there means an agent is silently running cold;
a 49-byte plain file containing the target path was exactly the 2026-06 corruption in
mosaic-web-app):

```bash
find "$WS" -name agent-memory -not -type l -not -path "$WS/.claude/agent-memory" \
  -not -path '*/node_modules/*' -not -path '*/.consolidate-backups/*' 2>/dev/null \
  | grep -E '/\.claude/agent-memory$' ; echo "(any output above = a fragmented or corrupted root)"
```

**Re-run the consolidator** (idempotent, dry-run by default; it merges any re-fragmented roots back
and replaces them with symlinks — 14 had re-appeared in the 2 weeks after the 2026-06-17 migration):

```bash
WORKSPACE_ROOT="$WS" bash data/scripts/agent-memory-consolidate.sh            # dry-run, inspect plan
WORKSPACE_ROOT="$WS" bash data/scripts/agent-memory-consolidate.sh --apply   # then apply
```

A healthy apply ends with `verifying inode-equality… OK` and `migrated=0` on an immediate re-run.
If a plain FILE (not dir/symlink) sits at a `.claude/agent-memory` path, the consolidator skips it —
fix manually: `rm <path> && ln -s "$WS/.claude/agent-memory" <path>`.

**Trim oversized lessons files** (keeps per-dispatch read cost bounded):

```bash
bash data/scripts/milestone-pipeline-consolidate-memory.sh --status   # report sizes
bash data/scripts/milestone-pipeline-consolidate-memory.sh --all      # dedupe + trim to 500 lines
```

## 9. Known integrity notes (carry forward, don't rediscover)

- `kubevirt-rhel-vms-m3` is double-emitted in the 2026 corpus (`critique-complete` + `complete`,
  identical counts) — dedupe by `(id, max(emitted_at))` for per-milestone stats.
- One legacy record carries `rectification_commit: 10365000.0` (a SHA parsed as a float before the
  2026-06 string-preserving fix) — tolerated, documented in sealed-eval.md §7.
- Pre-2026-06-19 records used pass-count `rectification_count` semantics; later records use
  `len(fixed_findings)` — do not compare rect_count across that boundary.
