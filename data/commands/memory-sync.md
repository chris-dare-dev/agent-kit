---
description: "Promote a durable agent-memory lesson to the right tier — repo wiki Field Notes (gated), repo CLAUDE.md, or data/references — without duplicating it across tiers. Use when a pipeline agent captured a significant, durable lesson in .claude/agent-memory/<agent>/lessons.md that is useful beyond that one agent/run, or when the user says \"promote this lesson\", \"share this finding to the wiki\", \"make this canonical\". NOT for routine per-run calibration (leave it in lessons.md) and NOT for facts already recorded in CLAUDE.md or data/references (dedupe/update in place instead). The wiki write and any git push are gated external writes."
argument-hint: <repo> "<lesson>" [--tier wiki|claude-md|references] [--agent <name>]
type: command
status: active
tags:
  - type/command
  - status/active

---

# /memory-sync — promote a memory lesson to the right tier

Routes one durable learning to a SINGLE canonical tier per the memory-write protocol
(full model: `get_reference("agent-memory")` → "The wiki tier" + "memory-write protocol").
It does NOT duplicate the same content across tiers — that is the drift trap the
capability-scout internal-adversary warned about. Pick the lowest altitude that fits.

## When to use / when NOT to

| Use `/memory-sync` | Don't |
|---|---|
| An agent captured a **significant, durable** lesson in `.claude/agent-memory/<agent>/lessons.md` that is useful **beyond that one agent/run** | Routine per-run calibration — leave it in `lessons.md` |
| You want a repo finding visible to other agents/humans (wiki) or made canonical (CLAUDE.md) | Re-recording something already in CLAUDE.md / data/references (dedupe instead) |

## The tiers (pick ONE)

- **`wiki`** (default) — cross-agent, repo-scoped, human-browsable. Written to the repo's
  **Field Notes** via the gated `append_repo_field_note` MCP tool.
- **`claude-md`** — a canonical invariant/decision for the repo. Edit
  `platform/source/<repo>/CLAUDE.md` (Decision Records / Pitfalls).
- **`references`** — applies across repos. Edit `data/references/*.md` (queryable via
  `search_platform_knowledge`).

## Steps (run in the main thread)

1. **Parse** `<repo>`, the lesson text, optional `--tier` / `--agent`. If `--tier` is
   absent, classify: cross-agent-but-repo-scoped → `wiki`; durable repo invariant →
   `claude-md`; cross-repo → `references`. If genuinely ambiguous, ask the user.
2. **Dedupe FIRST** (on-demand, token-safe — never eager-load):
   - wiki → `search_repo_wiki({repo, query: <key terms>})`
   - claude-md → grep `platform/source/<repo>/CLAUDE.md`
   - references → `search_platform_knowledge(<key terms>)`
   If a near-duplicate exists, update it in place (or skip) — do NOT append a duplicate.
3. **Route + gate:**
   - **wiki:** call `append_repo_field_note({repo, entry, agent, dry_run: true})` →
     show the returned **preview + confirm_token to the USER** → on explicit approval,
     call again with `confirm_token` + identical args to write. Sub-agents cannot
     self-approve; the human approves the preview (External Write Policy).
   - **claude-md:** dispatch the **`context-curator`** agent to draft the CLAUDE.md edit;
     present the diff; on approval commit and push it directly to the trunk (`main`). Create an MR
     only when Chris explicitly requests a review gate.
   - **references:** draft the `data/references/` edit; present the diff; commit and push it directly
     to the trunk (`main`) after authorization. (Per the workspace "Sharing changes" rule, a
     `data/` change is only live for the team once it reaches `main`.)
4. **Back-pointer:** annotate the source `lessons.md` entry with the promotion, e.g.
   `(promoted → wiki Field Notes 2026-06-06)`, so the same lesson is not re-promoted.

## External-write boundary

Every tier's SHARE step is an external write — the wiki PUT (gated by the
dry-run→confirm-token round-trip) or a `git push`. STOP and get explicit per-event
confirmation. Never push or confirm-write without authorization.

## Anti-patterns

| Tempting | Reality |
|---|---|
| "Write it to wiki AND CLAUDE.md AND references to be safe." | That's the drift trap. ONE altitude. Promote upward later if it earns it. |
| "Auto-confirm the wiki write since I just minted the token." | No. The confirm_token is content-binding + single-use; the *human approval* is the gate. |
| "Eager-load the wiki to check for dupes." | Use `search_repo_wiki` (snippets, on-demand). Eager-load breaches the token gate. |
| "Promote every lesson." | Only significant + durable + cross-scope ones. Routine calibration stays in `lessons.md`. |

## Related

- `get_reference("agent-memory")` — the memory architecture + promotion flow (source of truth).
- `append_repo_field_note` / `get_repo_wiki` / `search_repo_wiki` — the wiki MCP tools.
- `context-curator` — the agent that curates CLAUDE.md + the user memory tree (dispatched for the `claude-md` tier).
