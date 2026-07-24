---
project: agent-kit
type: handoff
status: complete
authorship: agent-generated
tags:
- project/agent-kit
- type/handoff
- authorship/agent-generated
---

# Handoff contract — continuation + review session handoffs

The single source of truth for session-handoff documents: filename grammar, frontmatter schema,
body templates, the roadmap review-checkpoint convention, and how the Obsidian vault tooling
consumes each field. The `/handoff` skill authors against this contract;
`.claude/scripts/handoff-validate.py` enforces it.

## 1. The two kinds

| Kind | Audience | Purpose | Filename suffix |
|---|---|---|---|
| **continuation** | The next builder session (any provider) | Resume exactly where this session stopped: state table, RESUME-HERE step, gates, environment reconnect notes | `-continuation.md` |
| **review** | An external principal-engineer reviewer — ChatGPT-5.6 Sol Ultra by default, a high-effort Fable session as backup | Independent audit of everything shipped across the session's milestones (typically 3–4 milestones / 1–2 epics): correctness, safety, honesty of "done" claims, coding practices, and program direction | `-session-review.md` |

Generate **both** at the end of any session that shipped milestone-scale work; generate only a
continuation for mid-stream or light sessions. The two documents cross-link via the `companion:`
key and a body link. A review handoff without a companion continuation is a smell (the reviewer
finds problems; the next builder needs somewhere to resume from).

## 2. Storage + filename grammar

- **Canonical location:** `<workspace-root>/plans/` (the vault-root plans region). Handoffs are
  session artifacts — **local-only, never committed** to any repo (same rule as roadmaps/spike
  artifacts).
- **Filename:** `HANDOFF-<YYYY-MM-DD>-<project-slug>[-<detail>]-<continuation|session-review>.md`
  - The **project slug must appear as a hyphen-bounded segment** — the vault project-linker claims
    handoffs by filename (segment-bounded slug match against `scripts/project-map.json`), not by
    content. An unclaimed handoff never surfaces in the project hub or the Home Workstreams list.
  - `<detail>` is optional scope info (e.g. `-e5m2-e6b`); keep the suffix literal.
- **Claim rule:** the filename must be claimed by at least one project in
  `<workspace-root>/scripts/project-map.json` (slugs / `contains` tokens, `excludes` wins).
  `handoff-validate.py --claim-name <filename>` answers "who claims this?" before you write. If
  nothing claims it, either use a claimed program slug in the name or add a `contains` token to
  the owning project in `project-map.json` (a local, ungated edit).

## 3. Frontmatter schema

Keys marked (R) are review-only, (C) continuation-only. Everything else applies to both.

```yaml
---
authorship: agent-generated
type: handoff                  # EXACTLY this — Handoffs.base filters note.type == "handoff"
handoff_kind: continuation     # continuation | review — must match the filename suffix
project: <project-or-program-slug>   # Bases group by this; use the roadmap slug
date: 2026-07-12               # == the date in the filename
status: complete               # doc lifecycle (the handoff itself is finished when written)
companion: <other-handoff-filename.md>   # the paired handoff; omit only if none exists
roadmap: <workspace-relative path to the roadmap .md>   # required for review, recommended for continuation
resume_target: fable           # (C, optional) fable | opus | any
reviewer_target: gpt-5.6-sol-ultra   # (R) or: fable
review_status: requested       # (R) requested | in-review | verdict-received | closed
milestones_covered:            # (R) roadmap milestone ids covered by this review
  - <slug>-m1
  - <slug>-m3
tags:
  - type/handoff
  - project/<project-slug>
  - handoff/<kind>
  - authorship/agent-generated
  - review/requested           # (R)
aliases:
  - "<project> — <kind> handoff (<date>)"
---
```

**Why each key is load-bearing** (the vault tooling this feeds):

- `type: handoff` — `Notes/Bases/Handoffs.base` filters `note.type == "handoff"`. The
  project-linker **copies** each project's latest handoff into the indexed
  `Notes/Projects/<proj>/handoffs/` folder, so this frontmatter directly controls Base membership.
  Legacy values like `session-handoff` make the handoff invisible to the Base — never use them.
- `project:` — Bases group/filter by it (`note.project == "…"`). Use the program/roadmap slug
  consistently across the roadmap doc and both handoffs so they land in the same group.
- `date:` / `status:` — `obsidian-stamp.py` would derive these; writing them explicitly (stampers
  never overwrite) keeps the values deterministic.
- `handoff_kind`, `review_status`, `milestones_covered` — machine-readable hooks for future Base
  views (e.g. a "reviews requested" queue filters `note.review_status == "requested"`).
- Filename (not frontmatter) drives project-hub claiming — see §2.

**Secrets rule:** handoffs regularly carry reconnect context (profiles, contexts, SM paths). Refer
to secrets **by Secrets Manager path / env-var name only** — never a literal value. The
plaintext-secret write hook and the validator's secret sniff both flag violations.

## 4. Body template — continuation

```markdown
# CONTINUATION HANDOFF — <project> (<date>)

> **Audience:** a fresh <resume_target> session picking up <project>. The companion review handoff
> ([[<companion>]]) covers *what shipped and why* — THIS doc says **exactly where to resume and
> what's left**. Roadmap: `<roadmap path>`.
>
> **Program goal:** <one sentence>.

## 1. Current state (as of this handoff)

| Phase / milestone | Status |
|---|---|
| <id — name> | ✅ SHIPPED / ⬜ ← RESUME HERE / ⬜ next |

<One line per load-bearing live fact: what is running where right now.>

## 2. RESUME HERE — <the exact next story/step>

**Goal:** <one sentence>.
<The load-bearing facts already decided. The exact commands/diffs to run, gates included
("X is a GATED external write — present and confirm before applying").>

## 3. Definition of done for the in-flight milestone

<Closure checklist: critique pass, roadmap checkbox tick, memory/hub updates.>

## 4. Remaining epics / milestones

<Per epic: one short paragraph — scope, gate to advance, carry-forward gotchas.>

## 5. Cross-cutting follow-ups (landmines you'll trip on)

<Numbered. Things NOT in this program that will bite a fresh session.>

## 6. Environment / resume notes (how to reconnect)

<AWS profiles, kubectl contexts, tokens (by location, never value), state backends,
pipeline state paths, `--resume` invocations.>

## 7. Key values you'll need (copy-paste reference)

```
<key>: <value>        # paths, DNs, image pins, ArgoCD app names — NO secrets
```

*Full review of what shipped: [[<companion>]].*
```

## 5. Body template — review

```markdown
# HANDOFF (REVIEW) — <project> session, <date>

> **Audience:** a <reviewer_target> review session. **Goal:** independently scrutinize everything
> shipped this session — correctness, safety, whether the "done" claims are honest, the coding
> practices, and the program direction — against the diffs (and live state where applicable).
> This is a REVIEW handoff (find problems); the companion continuation handoff ([[<companion>]])
> is for the next builder. Roadmap: `<roadmap path>`.

## 0. TL;DR — what this session did

| # | Work | Repos touched | Key SHAs (branch) | State |
|---|---|---|---|---|
| 1 | <milestone/work item> | <repos> | <shas> | SHIPPED / LIVE / DORMANT |

<One paragraph: the session narrative, and which items are live-behavior changes vs dormant.>

## 1..N. <One section per work item>

<What was done and why. Design decisions with their rationale. Files + SHAs.>

### What to SCRUTINIZE
<The specific claims the reviewer should try to break — per item. This subsection is MANDATORY
for every work item; a review handoff that doesn't tell the reviewer where the bodies might be
buried is marketing, not a review request.>

## N+1. Cross-cutting durable gotchas + decisions

<Numbered — everything a reviewer needs to avoid false positives (platform invariants,
known-accepted tradeoffs, VEX suppressions, push-order constraints).>

## N+2. Verification evidence (as of handoff)

<Tests/CI/live-verify status per repo, including anything still in flight ("tag-bump queued —
reviewer: confirm it landed"). Be honest about what was NOT verified.>

## N+3. How to review (repro + response contract)

- **Diff access:** for each repo — path, branch, SHA range (`git log --oneline <from>..<to>`).
- **Review axes:** (1) correctness/safety of each change; (2) honesty of the done-claims against
  evidence; (3) coding practices (idioms, tests, blast radius); (4) program direction — is the
  roadmap's next step still right given what shipped?
- **Calibrate the verdict to the milestone's state:** a dormant/flag-off mechanism is judged on
  "safe to activate later + honestly labeled", NOT on "not yet activated". (Past reviews have had
  correct facts but a miscalibrated verdict by ignoring dormancy.)
- **Response format:** per-finding — severity (CRITICAL/HIGH/MED/LOW), the claim it refutes,
  evidence (file:line / command output), suggested disposition. End with an overall verdict:
  SHIP / SHIP-WITH-FIXES / NO-GO, scoped per milestone.
```

## 6. Roadmap review checkpoints (review handoffs only)

Every review handoff adds an **optional audit task** to the program's roadmap so the roadmap shows
where a session ended and where an external audit is worth running.

- **Section:** `### Review checkpoints` — created on first use, placed immediately before
  `## Revision history` when present, else appended at end of doc.
- **Line format** (one per review handoff):

```markdown
- [ ] (optional) session audit <date> — covers `<slug>-mA`, `<slug>-mB`, … · handoff: `plans/<review-handoff-filename>` · reviewer: <reviewer_target>
```

- Tick `- [x]` when the audit verdict has been received **and dispositioned** (findings fixed or
  waived) — at that point also flip the handoff's `review_status:` to `closed`.
- **Insert via the tool, not by hand:** `handoff-validate.py --insert-checkpoint` writes this
  idempotently and keeps the format parser-safe.
- **Board rendering (vault-local):** on the Obsidian roadmap-status board
  (`scripts/roadmap_status_excalidraw.py`, Chris's machine) each checkpoint renders as a distinct
  **red audit-flag card** below that roadmap's milestone cards — a raised bright-red flag (⚑) when
  the audit is open, a lowered muted flag (⚐) once dispositioned — so a reader sees where a session
  ended and where an audit is worth running. This is the visual payoff of the checkpoint; the card
  is a distinct class, never a milestone. (Vault tooling is local-only; engineers without it still
  get the checkbox line in the roadmap markdown.)
- **Parser-safety rules** (the vault roadmap-status board + portfolio projection parse roadmap
  docs; these lines must never register as milestones or leak status into one):
  - never bold a milestone id (`**M3:**`) or start the line with a bold backtick slug;
  - start the line exactly with `- [ ] (optional) session audit`;
  - keep the section heading text exactly `Review checkpoints` (the status-board parser treats it
    as a section break, so these checkboxes can't be mis-attributed to a preceding milestone).

## 7. Generation checklist (what `/handoff` does)

1. Inventory the session: repos touched, commits (`git log`), live changes, milestones covered,
   open threads, roadmap path.
2. Resolve the project slug + filenames; pre-flight the claim (`--claim-name`).
3. Write the handoff(s) to `<workspace-root>/plans/` from the templates above, cross-linked.
4. Review kind: insert the roadmap review checkpoint (`--insert-checkpoint`).
5. Validate every file written: `handoff-validate.py --file <path>`.
6. Best-effort vault surfacing (Chris's machine has this; others may not): if
   `<workspace-root>/scripts/project-linker.py` exists, run `frontmatter-stamp.py --file` then
   `project-linker.py --file` on each handoff. Skip silently when absent.
7. Emit one best-effort append-only ingestion receipt for the finalized handoff set with
   `scripts/artifact_skill_capture.py emit --producer handoff --run-id <stable-run-id> --path ... --apply`.
   This records Qdrant eligibility and Graphiti candidate status; it writes neither sink. Because
   receipt emission follows file validation and vault projection, an in-flight filesystem observer
   may briefly see the Markdown before its receipt. Preserve the handoff and report the failure if
   capture is unavailable; never rewrite or delete the source to repair indexing.
8. Report: paths, claiming project, ingestion receipt status/path/event id, the checkpoint line,
   and — for review handoffs — the exact
   dispatch instruction for the reviewer session.
