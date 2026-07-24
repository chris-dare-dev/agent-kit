# Live roadmap status boards

Each project gets `Notes/Projects/<project>/<project>-roadmaps.excalidraw.md` — a live, fixed-width
**portfolio pulse** generated from canonical roadmap and review-handoff documents. The canvas is
bounded to 1,440 px and grows vertically, so even a 21-roadmap project remains readable inside an
Obsidian hub without horizontal panning.

Roadmaps are grouped by attention rather than filename alone:

1. **Focus** — open/drifted reviews or an explicitly active item;
2. **Underway** — some work is done and more remains;
3. **Queued** — planned but not started;
4. **Complete** — every tracked item is done;
5. **Archive** — cancelled or superseded, excluded from totals.

Layout density adapts to project size: small projects use two detailed columns (up to four pulse
items), medium projects use three columns (two pulse items), and projects above 12 roadmaps show the
single highest-priority pulse item. Complete/archive history collapses further into a four-column
summary grid on larger boards. The roadmap header and every pulse card link to the
canonical source. Blue surfaces mean **milestone**, violet means **spike**, and rose means **review**;
status is a separate green/amber/slate rail and glyph. Cards use square corners and place bound text
inside a separate container offset 24 px from the flush rail, before Excalidraw's own text padding, so plugin
normalization cannot move lifecycle glyphs back onto the accent. This keeps
type and delivery state orthogonal. The `_index.md` hub also mirrors full status as a non-task icon list (`✅` / `🟡` / `⚪`).
Only authoritative roadmap sources contain checkboxes, so the Tasks plugin does not count generated
status copies as human actions.

## The milestone-status convention (canonical = checkbox)

A milestone or spike is marked **done by ticking its checkbox**:

```markdown
#### M1: Build the thing — milestone ID `proj-m1`
- [x] complete            ← done

- [ ] **M2:** Next thing  ← pending
- [/] **M3:** In progress ← in-progress  (also [~] or [-])
```

The parser (`scripts/roadmap_status_excalidraw.py`) is **lenient** so existing roadmaps work with
zero retrofit — it reads, in priority order:
1. a checkbox on the milestone's line — `[x]` done · `[/]`/`[~]`/`[-]` in-progress · `[ ]` pending;
2. legacy markers — `✅` / `SHIPPED` / `DONE` / `COMPLETE` / `LIVE` = done; `🚧` / `WIP` / `IN PROGRESS` = active.

Parsed data keeps a semantic title plus the full source subject, canonical id when available, type,
epic, Now/Next/Later lane, and source order. Wide pulse cards shorten only extreme display titles
(96 characters); project hubs and portfolio projections remain lossless.

Discovery deliberately ignores `HANDOFF-*` files even when their descriptive filename contains
the word `roadmap`; a handoff may report roadmap progress but is never itself an execution board.
Roadmaps whose frontmatter `status` is `cancelled` or `superseded` remain visible for history but
are excluded from project completion numerators and denominators.

## Review handoffs

The review rail is a separate projection and never contributes to milestone completion. A parser-safe
roadmap checkpoint binds the review identity, but the linked handoff frontmatter is authoritative for
the four-state lifecycle:

`requested → in-review → verdict-received → closed`

The roadmap checkbox is only a terminal projection (`[x]` iff `review_status: closed`). The renderer
compares the handoff and checkpoint date, reviewer, scope, roadmap backlink, and checkbox. Any mismatch
renders as **CONTRACT DRIFT**; a missing/malformed handoff or missing checkpoint renders as
**UNRESOLVED**. Review cards link directly to the review handoff's portable presentation alias.
Attributable legacy `*session-review.md` files without a checkpoint are deliberately surfaced as
unresolved rather than disappearing from the roadmap UI.

A milestone id may appear on several lines (a detailed `#### M1:` heading + a later `- **M1:** … ✅`
bullet); the parser takes the **best** status seen across all of them.

**Agents:** when you complete a milestone, tick its checkbox (or add `✅`/`— SHIPPED`). When `/roadmap`
creates a roadmap, emit each Now-lane milestone with a `- [ ]` checkbox so status is machine-trackable.

## Live updates
- **Instant:** the PostToolUse hook (`project-linker-hook.sh`) regenerates a project's board the moment
  an agent edits its roadmap `.md` (via `project-linker.py` → `sync_project` → the board builder).
- **15-min backstop:** `com.workspace.roadmap-board-refresh` LaunchAgent runs `roadmap-board-refresh.sh`
  every 900s for edits made outside Claude Code.
- **No idle churn:** the board stamps a `roadmap-board-sig` over every rendered semantic (roadmap
  status/title, item identity/type/status, review lifecycle/binding state), its renderer schema,
  stable project identity, and portable source links. It is rewritten only when the rendered result
  should change, using an atomic same-directory replace. A linked scene-fingerprint element carries
  that same signature through Excalidraw compression; if an open Obsidian tab later writes an older
  scene over fresh frontmatter, the next refresh detects the mismatch and replaces the stale scene.

## Portable navigation

Every roadmap header and milestone card links to the roadmap's canonical presentation-vault alias:

`obsidian://open?vault=Vault&file=Notes/Projects/<project>/_sources/<region>/<roadmap>.md`

The vault name and roots come from `scripts/project-map.json`; cards never embed the source
workspace's absolute `/Users/.../Work/workspace/...` path. Generated board frontmatter also carries the
stable `project_id` and presentation-vault name for downstream validation.

## Drift guard (lint)

A milestone authored with **no status line at all** (a bare `#### M3: …` spec that leads straight into
`- **Source epic:**`) silently defaults to `pending` — so a *completed* milestone that never had its
checkbox added reads as not-done and the board **undercounts**. This is the failure mode that hid ~15
finished Zero-Trust milestones (board showed 62% when the true figure was 86%).

The guard flags every milestone/spike whose section carries no explicit `- [ ]` / `- [/]` / `- [x]` (or
legacy marker). It reuses the board parser's own detection (`subject` + `resolve_status`), so it can
never disagree with what the board renders. Make "pending" an **explicit** `- [ ]` declaration, not a
silent default.

```
python3 scripts/roadmap_status_excalidraw.py --lint <roadmap.md>  # one file (exit 1 if any violation)
python3 scripts/roadmap_status_excalidraw.py --lint-all           # every discovered roadmap (exit 1 if any)
```

The 15-min backstop (`roadmap-board-refresh.sh`) runs `--lint-all` each cycle into
`.claude/notes/vault-restructure/roadmap-status-lint.log` (non-fatal), so new drift surfaces within 15
minutes. `--lint-all` also exits non-zero, so it drops straight into a pre-commit hook or CI check.

## Run manually
```
python3 scripts/roadmap_status_excalidraw.py --reconcile          # all projects
python3 scripts/roadmap_status_excalidraw.py --reconcile --force  # replace every scene, even if signed
python3 scripts/roadmap_status_excalidraw.py --project the dispatcher service
python3 scripts/roadmap_status_excalidraw.py --parse <roadmap.md> # debug the parsed status
```
