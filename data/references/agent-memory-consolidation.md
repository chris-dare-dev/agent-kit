---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Agent-Memory Root Consolidation (the symlink-farm)

**Milestone:** `self-improving-tooling-m2` (E2 / KR1 enabler). **Status:** built + fixture-proven;
the LIVE migration is a separate gated step (see §6).

This doc explains how the fragmented agent-memory substrate is consolidated to ONE unified root
**without editing a single agent body**, why that is the safe design, and how to run / roll back the
migration.

---

## 1. The problem — dozens of cwd-fragmented roots

A live `find <workspace> -type d -name agent-memory -not -path '*/node_modules/*'` finds **~40+**
distinct `.claude/agent-memory` directories — one per repo (charts/, source/, infra/, tools/,
ci-cd-templates/), plus the workspace root, plus sub-dir noise (`plans/`, `…/poc/`, `…/l3-compute/`).
The exact count **drifts** as pipelines run in new repos (each run that writes a lesson can create a
fresh root), so treat it as a ballpark, not a fixed number — the dry-run prints the live
`discovered=N` (§6). Each root holds a **divergent** copy of each agent's `lessons.md`, because an
agent's cwd at dispatch time decides which `.claude/agent-memory` its hardcoded `cat`/`>>` lands on.

Concretely: the `milestone-researcher` corpus had **19 copies with 19 distinct md5s** — every copy
diverges, ranging from 1 line to 300 lines. So cross-run / cross-repo learning (E3) has no single
corpus to read; it has dozens of fragments. The fix is to make every one of them resolve to ONE store
**and** merge the existing divergent copies into it losslessly.

## 2. Why NOT edit the 59 agent bodies (the BLOCKER)

59 agent definition files in `data/agents/` hardcode the **cwd-relative** path:

```bash
cat .claude/agent-memory/<name>/lessons.md 2>/dev/null || echo "(no lessons yet)"   # READ
mkdir -p .claude/agent-memory/<name>                                                # WRITE
echo "$(date …) <lesson>" >> .claude/agent-memory/<name>/lessons.md
```

(A few use a `${REPO_ROOT}/.claude/agent-memory/…` prefix — still a `<repo>/.claude/agent-memory` path.)

Retrofitting all 59 to source an `$AGENT_MEMORY_ROOT` resolver would be **59 coordinated edits across
~10 execution contexts with a silent failure mode**: an unset env var → empty read → the agent runs
cold with NO error. That is the rejected approach. We change ZERO bodies.

## 3. The safe design — the symlink-farm

Replace each legacy `.claude/agent-memory` **directory** with a **symlink** to one unified
root. The kernel resolves the symlink before the agent's `cat`/`>>` ever sees a path, so the existing
hardcoded cwd-relative IO transparently hits the unified store:

```
Pre :  charts/velero/.claude/agent-memory/  (real dir, small divergent copy)
Post:  charts/velero/.claude/agent-memory   --> $WORKSPACE_ROOT/.claude/agent-memory   (symlink)

# same unchanged agent body:
cat .claude/agent-memory/milestone-researcher/lessons.md
#   -> charts/velero/.claude/agent-memory (symlink) -> $WORKSPACE_ROOT/.claude/agent-memory
#   -> /milestone-researcher/lessons.md  ==  the unified, merged corpus
```

A subsequent `>> …lessons.md` appends to the unified file — so cross-repo runs now **accumulate** into
one corpus (the E3 goal). `mkdir -p .claude/agent-memory/<name>` through the symlinked parent creates
the leaf inside the unified root. The `${REPO_ROOT}`-prefixed variant resolves identically.

**The invariant:** for every legacy root `L`, unified root `U`, and agent `A`,
`inode(L/A/lessons.md) == inode(U/A/lessons.md)` — same physical file, same inode.

## 4. The unified root + resolution order (`agent-memory-root.sh`)

**Default unified root:** `$WORKSPACE_ROOT/.claude/agent-memory` (the richest existing corpus and the
natural "above all repos" anchor; gitignored, so the corpus stays local/per-machine). Overridable via
`$AGENT_MEMORY_ROOT` — e.g. point it at `tools/claude-mcp-server/data/agent-memory` (the in-repo target
the repo `.gitignore` comment + the `roadmap-materializer` precedent already anticipate).

`data/scripts/agent-memory-root.sh` is a sourceable POSIX-sh helper for **NEW code** (the E3 trajectory
reader) and for the smoke test. Resolution order is **new-first / legacy-second**:

1. `$AGENT_MEMORY_ROOT` — explicit override (honored even if it doesn't exist yet; the migration
   creates it).
2. `$WORKSPACE_ROOT/.claude/agent-memory` — canonical default, **only if it exists on disk** (an empty
   new root can't shadow a populated legacy corpus).
3. the legacy cwd-root (`./.claude/agent-memory` by default) — **back-compat fallback**.

The fallback is the orphan-prevention guarantee: if the override is unset AND the canonical default is
absent, resolution lands on the legacy corpus (where the lessons physically are), **never on an empty
read** — degrading to the legacy corpus instead of the BLOCKER's silent cold-start. NB: the existing 59
bodies are fronted by the **symlink-farm**, not by sourcing this helper (they can't source it without a
body edit). The helper exists so new code has a sanctioned resolver and so the S2.2 test can exercise
the fallback branch deterministically.

Usage:

```sh
AMR_SOURCED_ONLY=1 . data/scripts/agent-memory-root.sh
dir=$(agent_memory_root milestone-researcher)        # -> <base>/milestone-researcher
base=$(agent_memory_base)                            # -> <base>
# or as a CLI:
bash data/scripts/agent-memory-root.sh milestone-researcher
bash data/scripts/agent-memory-root.sh --base
```

## 5. The migration (`agent-memory-consolidate.sh`) — merge + symlink-replace

stdlib/bash + `awk`/`tar`/`find` only. **Dry-run by default; `--apply` to mutate; backup before any
mutation; idempotent.**

```
agent-memory-consolidate.sh [--apply] [--root <unified>] [--workspace <dir>]
                            [--backup-dir <dir>] [--quiet]
```

Per discovered legacy root:

1. **BACKUP** — tar the whole root to `<backup-dir>/<sanitized-path>.tar.gz` before touching it.
2. **MERGE** — for each agent subdir, concat+dedup every `*.md` INTO `unified/<agent>/<file>` (see
   §5a).
3. **SYMLINK-REPLACE** — `rm -rf <legacy-root>; ln -s <unified> <legacy-root>`.
4. **VERIFY** — assert `inode(legacy/<agent>/lessons.md) == inode(unified/<agent>/lessons.md)` for
   every migrated root (the symlink-farm invariant; the script fails non-zero on any mismatch).

Skips (the safety guards): the unified root itself; any root **inside** the unified root (loop guard,
e.g. the backup dir); any root the unified root lives **inside** of; roots **already symlinked** to the
unified root (idempotency); and symlinks pointing at a *non-unified* target are left untouched (no
clobbering a deliberate alias).

### 5a. CONCAT + DEDUP — provably loses no lesson

The corpus is line-oriented append-only (`>> lessons.md`, one dated lesson per line). The merge:

- Concatenates the existing `unified/<file>` (first, so its order wins) + every legacy copy, then
  **dedups by EXACT line equality** (after trailing-whitespace trim only), preserving first-seen order,
  collapsing runs of blank lines to one. Pure `awk '!seen[$0]++'` = stdlib.
- **Exact-line, never semantic.** Only byte-identical lines collapse (true duplicates of the same
  lesson written in two repos). Two lessons differing by even one char **both survive** — the "NEVER
  drop a lesson" lock. The agents' own semantic self-consolidation pass (`agent-memory.md`) handles
  fuzzy merge later; the migration must not pre-empt it.

### 5b. Idempotency

Re-running is a no-op: roots already symlinked to the unified root are skipped, and exact-line dedup is
set-union (re-merging the same content changes nothing). Safe after a partial/interrupted run.

## 6. How to run the LIVE migration (the separate gated step)

The implementer built + fixture-proved this; it did **NOT** run the migration on the live roots —
that mutates local (gitignored, per-machine) data and is a decision the operator/main session owns.
To perform it:

```bash
cd "<workspace>"   # so $WORKSPACE_ROOT-relative defaults apply
S="GitLab/workspace/platform/tools/claude-mcp-server/data/scripts/agent-memory-consolidate.sh"

# 1. DRY-RUN first — read the plan. The summary prints `discovered=N`; confirm N is in the
#    expected ballpark (~40+, drifts as pipelines touch new repos) and the unified target is
#    right — NOT that it equals any fixed number.
bash "$S" --workspace "$PWD" --root "$PWD/.claude/agent-memory"

# 2. APPLY — backs up every root, merges losslessly, installs the symlink-farm, verifies inodes.
bash "$S" --apply --workspace "$PWD" --root "$PWD/.claude/agent-memory"
```

It is safe to re-run `--apply` (idempotent). The migrated corpus is **local-only and NOT committed**
(agent-memory is gitignored). Only the scripts + this doc are the committed deliverable.

## 7. Rollback

Every migrated root was tarred before mutation. To restore one root:

```bash
rm "<legacy-root>"                                   # remove the symlink
tar -xzf "<backup-dir>/<sanitized-path>.tar.gz" -C "<parent-of-legacy-root>"
```

The unified corpus is a strict superset of every legacy copy (lossless merge), so restoring a legacy
root never recovers a lesson the unified store lacks — rollback is for reverting the *structure*, not
recovering data.

## 8. Acceptance gate — the smoke test (S2.2)

`data/scripts/agent-memory-consolidate-test.py` (stdlib `unittest`) builds a SYNTHETIC fixture farm in
a tempdir (never the live roots) and asserts, on the fixture:

- **inode-equality** — every fixture legacy root resolves to the same unified inode;
- **no-loss** — the union of all pre-migration distinct lines ⊆ the merged corpus (the divergent-copy
  merge drops nothing);
- **dedup** — a deliberately-shared line appears exactly once after merge;
- **backup** — one tarball per migrated root, round-trips via `tarfile`;
- **idempotency** — a second `--apply` is a no-op;
- **fallback** — a deliberately-missed legacy path exercises the back-compat resolution branch (NOT the
  happy path), proving orphan-prevention is **tested, not assumed**; and the override happy-path is also
  asserted.

Run: `python3 data/scripts/agent-memory-consolidate-test.py`.

## 9. Files

| File | Purpose |
|---|---|
| `data/scripts/agent-memory-root.sh` | sourceable resolution-order helper (new-first / legacy-second) for NEW code + the fallback test. |
| `data/scripts/agent-memory-consolidate.sh` | the migration: enumerate → backup → concat+dedup merge → symlink-replace → inode-verify. `--dry-run` default, `--apply`, `--root`, `--workspace`, `--backup-dir`. Idempotent. |
| `data/scripts/agent-memory-consolidate-test.py` | the S2.2 smoke test (synthetic fixture farm). |
| `data/references/agent-memory-consolidation.md` | this doc. Cross-linked from `agent-memory.md`. |

See also `data/references/agent-memory.md` (the canonical memory-model doc) and the roadmap
`plans/self-improving-tooling-roadmap.md` (M2 / E2).
