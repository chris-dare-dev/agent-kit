# project-linker — per-project consolidation of scattered `.md`

**Problem it solves.** A single project (e.g. the dispatcher service) branches across regions —
vault-root `plans/` + `docs/`, the GitLab `platform/plans/` + `platform/docs/`, a root
deliverables dir, and human handoffs in `Notes/<project>/`. The Obsidian ignore-filter hides
the code/agent trees from the index, so the scattered files are invisible in the vault.

**What it does** (per `scripts/project-map.json`):
1. **Symlinks** each matching file into `Notes/Projects/<project>/_sources/<region>/`
   (the canonical presentation-vault alias as well as a filesystem consolidation surface).
2. **Generates** `Notes/Projects/<project>/_index.md` — a real, **Obsidian-indexed** hub note
   mapping every file through its local `_sources/` alias. Generated hub links never walk out to
   `../../plans` or `../../GitLab`, so they resolve from the active presentation vault.
3. **Preserves** existing symlinks by default, including stale and wrong-target
   entries. Destructive maintenance requires `--allow-delete`.

Matching uses the project's filename `slugs` and optional `contains` substrings. Optional
`excludes` substrings are evaluated first and are the explicit ownership override for a
more-specific workstream that would otherwise match two broad domains. Agent files mostly lack
frontmatter tags, so filename signals remain load-bearing — tune all three lists in the manifest
as projects evolve.

## Single owner per source

A filename can carry two project slugs (`HANDOFF-…-svcreg-kargo-prod-…`), so more than one project
may claim it. **Exactly one owns the projection**; every other claimant gets a *cross-reference
note* instead of a second alias. Two aliases to one canonical file is the vault validator's
`DUPLICATE_MARKDOWN_ALIAS` error class — a structural defect, since the handoff naming contract
requires a project slug and cross-cutting work legitimately names two.

Ownership resolves in order — first decisive step wins:

1. `excludes` — unchanged, highest-precedence negative override.
2. frontmatter **`vault_owner: <project_id>`** — the authoritative per-file declaration.
3. manifest **`owns: [<basename>, …]`** — for legacy files carrying no frontmatter.
4. **earliest matching signal** in the filename; ties by longest signal, then manifest order.

Step 4 is deliberately *not* "first slug in the manifest", which would make ownership a function of
dict insertion order — reordering `project-map.json` would silently repoint projections. It also
matches the naming contract, where the leading workstream slug is the primary project.

The secondary's cross-reference note occupies **the same vault path** the alias would have. That
path is a link target for the generated hub, the Excalidraw status board, and hand-authored
Canvases, so removing it would merely trade duplicate-alias errors for unresolved-link errors.
Keeping the path with different content clears the duplicates and changes nothing else.

`--check-ownership` is a read-only gate that fails if any source would be aliased twice; it runs in
`vault-reconcile.sh` and is covered by `tests/test_project_linker_ownership.py`. Legacy duplicate
aliases are preserved by default and converted only under the narrow `--repair-duplicates` flag (or
the broad `--allow-delete`). Full rationale:
`plans/DESIGN-2026-07-18-vault-projection-single-owner-rule.md`.

## Stable identity and presentation-vault contract

`project-map.json` deliberately separates three concepts:

- `projects.<display name>.project_id` is the required, unique, stable kebab-case identity used in
  frontmatter and downstream indexes. It does not change when discovery rules change.
- `slugs` / `contains` / `excludes` are filename discovery aliases only. In particular, the first
  slug is no longer treated as project identity.
- `presentation_vault` names the active Obsidian vault and its vault-relative project/source-alias
  roots. Its filesystem `root` is for local validation and vault construction; generated navigation
  uses the vault name plus vault-relative paths, not that absolute root.

The generated project hub records `project_id`, `display_name`, `presentation_vault`,
`presentation_path`, and `source_alias_root` in frontmatter. A source link has the portable form
`_sources/<region>/<basename>` relative to that hub.

## Usage
```
python3 scripts/project-linker.py --reconcile            # all projects, create/preserve + regen hubs
python3 scripts/project-linker.py --project the dispatcher service     # one project
python3 scripts/project-linker.py --file <path.md>        # link one file (hook mode)
python3 scripts/project-linker.py --check-ownership       # read-only: no source aliased twice
add --dry-run to preview without touching disk
add --allow-delete only for reviewed destructive maintenance
add --repair-duplicates to convert legacy duplicate aliases into cross-reference notes
```

## Auto-trigger
`scripts/project-linker-hook.sh` is registered as a **PostToolUse (Edit|Write) hook** in
`.claude/settings.json`. After any Claude Code session writes a `.md`, it re-syncs that file's
project (detached, never blocks the write). Non-`.md` and non-project files are no-ops.
**Settings-hook changes take effect on the next session / settings reload.**

## Backstop (optional)
The hook only catches files written *by Claude Code*. For files created by CI or other tools,
run `--reconcile` periodically. Scheduled and hook modes preserve stale presentation entries.

## AgentDocs alias dedupe

`build-agent-vault.sh` now creates only policy-allowlisted curated Markdown beneath
`AgentDocs/`. Historical GitLab, `.claude`, and root `docs/` aliases remain in place but
are not recreated by the default-deny plan. A selected curated source may
therefore have both a generic farm alias and the canonical project `_sources/` alias above. The
builder now asks `agentdocs_alias_dedupe.py` for a safe exclusion set **before** linking, so a normal
refresh stays idempotent and does not recreate then prune the same duplicates.

An `AgentDocs` destination is suppressed only when an existing `_sources` symlink resolves to the
same live file and no indexed Markdown/Canvas/Base/Excalidraw file or `.obsidian` state file names
the farm path. Existing non-symlinks, broken links, changed targets, and referenced aliases are
preserved. Audit the current vault without changing it:

```bash
python3 scripts/agentdocs_alias_dedupe.py
python3 scripts/agentdocs_alias_dedupe.py --json
```

`--apply` is an explicit standalone cleanup path and is not called by scheduled automation. It unlinks only candidates that pass the same
gates and revalidates symlink ownership, target identity, and canonical-alias existence immediately
before each unlink. A normal `build-agent-vault.sh` refresh never prunes or replaces a path.

Existing root `the dispatcher service/` deliverable aliases are frozen during the transition.
Scheduled vault builds do not add or remove asset links.

## Companion: frontmatter-stamp.py (authorship tags)
`scripts/frontmatter-stamp.py` normalizes + authorship-stamps the scattered **agent-region**
`.md` (`plans/`, `docs/`, `platform/plans/`, `platform/docs/`):
- adds `authorship: agent-generated` + a `tags: authorship/agent-generated` entry,
- **fixes malformed YAML** (the mixed-indent `tags:` bug — a `- x` at col 0 mixed with `  - x`
  at col 2, which makes the block un-parseable and renders as raw text in Obsidian),
- prepends frontmatter to files that have none.
Idempotent, stdlib-only, conservative (skips inline-tags / no-closing-fence shapes).
```
python3 scripts/frontmatter-stamp.py --dry-run    # report only
python3 scripts/frontmatter-stamp.py              # apply to all agent-region .md
python3 scripts/frontmatter-stamp.py --file <p>   # one file; SELF-GUARDS to agent regions
```
It runs FIRST in the PostToolUse hook (before the linker), so new agent `.md` are auto-stamped.
The `--file` guard means the hook never stamps human notes (`Notes/`), config, or anything
outside the agent regions. Last full run: 531 files → 531 tagged, 0 invalid YAML, 73 malformed fixed.

## Legacy roadmap tree

`roadmap_tree_excalidraw.py` is a dormant legacy renderer and is not called by `project-linker.py`.
Its heuristic one-parent star does not encode real dependency semantics, so do not use it as the
operational roadmap view. The supported generated visual is the live status board documented in
`README-roadmap-status.md`; a future semantic Canvas replaces the legacy tree.

## Adding a project
Add an entry to `scripts/project-map.json` → `projects`:
```json
"My Project": {
  "project_id": "my-project",
  "slugs": ["my-slug"],
  "contains": ["legacy-project-name"],
  "excludes": ["my-slug-owned-elsewhere"],
  "deliverable_dirs": []
}
```
`project_id` must be globally unique. Then run
`python3 scripts/project-linker.py --project "My Project"`.
