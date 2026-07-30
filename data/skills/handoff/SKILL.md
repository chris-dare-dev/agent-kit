---
name: handoff
description: Generate rigorous session-handoff documents — a CONTINUATION handoff
  (the next builder session resumes exactly where this one stopped) and/or a REVIEW
  handoff (an external principal-engineer audit for a ChatGPT-5.6 Sol Ultra or high-effort
  Fable session covering the session's milestones). Enforces the canonical filename
  + frontmatter contract so the Obsidian vault tooling (Handoffs.base, project hubs,
  Home Workstreams) always claims and surfaces the doc, validates the result, and
  inserts an optional review-checkpoint task into the program roadmap so audits are
  visible where the work is tracked. Use at session end, when context is running long,
  when the user says 'handoff', 'wrap up the session', 'pass this to the next session',
  or 'get this reviewed'. NOT for milestone execution state (/milestone-pipeline owns
  that) and NOT for memory writes (/memory-sync).
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
type: skill
status: active
tags:
- type/skill
- status/active
- project/agent-kit
- type/handoff
- authorship/agent-generated
project: agent-kit
authorship: agent-generated
---

# Handoff

Author continuation and/or review handoffs against the canonical contract in
`.claude/references/handoff-contract.md`, validate them with
`.claude/scripts/handoff-validate.py`, and wire review handoffs into the roadmap as optional
audit checkpoints. Works identically from Claude Code, Codex, and OpenCode — the contract and
validator are provider-neutral; the Obsidian surfacing step degrades gracefully on machines
without the vault tooling.

## When NOT to use
- To record a durable lesson or preference — that's `/memory-sync` / memory, not a handoff.
- To advance or close a milestone — `/milestone-pipeline` owns execution state; a handoff only
  *describes* it.
- Mid-task context notes for yourself — handoffs are session-boundary artifacts.

## Arguments

`/handoff [continuation|review|both] [project-slug] [free-text notes]`

- **kind** — omit to decide from the session: shipped milestone-scale work (commits landed across
  ≥1 milestone) → `both`; mid-stream or light session → `continuation` only. Say which you chose
  and why in the final report.
- **project-slug** — omit to infer from the session's roadmap/milestone ids. This is the program
  slug (e.g. `identity-seed`, `svcreg-supplychain-trust`), used in the filename, `project:`
  frontmatter, and roadmap lookup.
- **notes** — anything the user wants emphasized (e.g. "reviewer should focus on the live flip").

## Steps

1. **Read the contract first**: `.claude/references/handoff-contract.md` — filename grammar,
   frontmatter schema, both body templates, the review-checkpoint format. Do not improvise
   frontmatter keys; legacy variants (`type: session-handoff`, `handoff-kind:`) break the vault
   Bases.

2. **Resolve the workspace root + tooling dir** (portable across providers/cwds — the second
   fallback is the kit repo's canonical path):
   ```bash
   WS="${PERSONAL_WORKSPACE_ROOT:-$HOME/Work/workspace}"
   [ -d "$WS/plans" ] || { echo "Set PERSONAL_WORKSPACE_ROOT to your the workspace" >&2; exit 1; }
   S="$WS/.claude/scripts"
   [ -f "$S/handoff-validate.py" ] || S="$WS/<workspace>/<project>/agent-kit/data/scripts"
   ```

3. **Inventory the session** (this is the substance — be thorough and honest):
   - Repos touched + commit ranges: `git log --oneline <base>..HEAD` in each repo the session
     committed to; note pushed-vs-unpushed.
   - Milestones/epics covered (roadmap ids), the roadmap doc path, and each milestone's honest
     state: SHIPPED / LIVE / DORMANT / in-flight (CI still running, tag-bump pending, …).
   - Live-behavior changes vs dormant mechanisms — reviewers and resumers both need the split.
   - Open threads, gates awaiting confirmation, landmines discovered (cross-cutting follow-ups).
   - What was verified vs what was claimed-but-not-verified.

4. **Name the file(s) and pre-flight the claim.** Filename:
   `HANDOFF-$(date +%Y-%m-%d)-<project-slug>[-<detail>]-<continuation|session-review>.md` in
   `$WS/plans/`. Verify the vault project-linker will claim it:
   ```bash
   python3 "$S/handoff-validate.py" --claim-name "HANDOFF-<...>.md"
   ```
   If unclaimed, prefer renaming to a claimed slug; otherwise add a `contains` token for the
   program to the owning project in `$WS/scripts/project-map.json` (local, ungated edit) and note
   it in the final report.

5. **Write the handoff(s)** from the contract's templates — continuation §4, review §5 — with the
   full frontmatter schema (§3). Both docs cross-link via `companion:` + a body link. Secrets by
   SM path / env-var name only, never literal values. Review handoffs: every work item gets a
   **What to SCRUTINIZE** subsection, and the final section gives the reviewer diff access
   (repo + branch + SHA range) and the response contract.

6. **Review kind only — insert the roadmap checkpoint** (idempotent, parser-safe):
   ```bash
   python3 "$S/handoff-validate.py" --insert-checkpoint \
     --roadmap "<workspace-relative roadmap path>" \
     --handoff "plans/HANDOFF-<...>-session-review.md" \
     --covers "<slug>-mA,<slug>-mB" --reviewer gpt-5.6-sol-ultra
   ```
   Roadmap docs are local-only artifacts — this edit needs no push gate.

7. **Validate every file written** (fix and re-run until clean):
   ```bash
   python3 "$S/handoff-validate.py" --file "$WS/plans/HANDOFF-<...>.md"
   ```

8. **Surface into the vault (best-effort).** On machines with the vault tooling this refreshes the
   project hub + Home Workstreams immediately (Claude Code's PostToolUse hook also does this;
   Codex/OpenCode sessions rely on this explicit step):
   ```bash
   if [ -f "$WS/scripts/project-linker.py" ]; then
     python3 "$WS/scripts/frontmatter-stamp.py" --file "$WS/plans/HANDOFF-<...>.md"
     python3 "$WS/scripts/project-linker.py"  --file "$WS/plans/HANDOFF-<...>.md"
   fi
   ```

9. **Emit one append-only ingestion receipt (best-effort, no sink write).** Pass every handoff
   written in this run as a separate `--path`. The run id must be stable for this handoff set
   (project + date + session purpose), not a newly generated timestamp:
   ```bash
   if [ -f "$WS/scripts/artifact_skill_capture.py" ]; then
     python3 "$WS/scripts/artifact_skill_capture.py" emit \
       --workspace "$WS" --producer handoff \
       --run-id "<project-slug>-<YYYY-MM-DD>-<session-purpose>" \
       --path "$WS/plans/HANDOFF-<...>-continuation.md" \
       --path "$WS/plans/HANDOFF-<...>-session-review.md" \
       --apply
   fi
   ```
   Omit the `--path` for a kind that was not requested. This records source hashes and routing
   intent under the per-machine artifact store; it does **not** write to Qdrant or Graphiti, and
   Graphiti bulk ingestion remains disabled. This step intentionally follows validation and vault
   projection, so another process may briefly observe the Markdown before its receipt. Never delete
   or rewrite a handoff to repair capture.
   If capture is unavailable or fails, keep the validated handoff and report
   `ingestion receipt: unavailable|failed`.

10. **Report** to the user:
   - paths written + which project claimed them;
   - ingestion receipt status and path/event id when one was recorded;
   - the roadmap checkpoint line (review kind);
   - the reviewer dispatch instruction (review kind): open a fresh **ChatGPT-5.6 Sol Ultra**
     (or high-effort Fable) session, provide the review handoff plus repo access, and ask for the
     response contract defined in the handoff's final section;
   - anything unclaimed/unverified you had to leave behind.

## Notes

- Continuation handoffs carry NO roadmap edit; only review handoffs create checkpoints.
- When the audit verdict later arrives and findings are dispositioned: tick the checkpoint
  `- [x]` in the roadmap and flip the review handoff's `review_status:` to `closed`.
- Handoffs are local-only session artifacts (like roadmaps/spike notes) — never commit them to a
  repo, never push them.
- One session, one date, one project → at most one continuation + one review; supersede by writing
  a newer-dated pair, not by editing history.
