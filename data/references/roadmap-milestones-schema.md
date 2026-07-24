---
type: reference
status: active
tags:
  - type/reference
  - status/active
---
# Roadmap milestone-objects schema (v1) — `.claude/notes/roadmaps/<slug>/milestones.json`

The machine-readable companion to `plans/<slug>-roadmap.md` (the "register"). It mimics a
regulated, local-first GitLab-issue store: one object per milestone with status,
dependencies, epic membership, and tags — so pipelines can *enforce* sequencing instead
of reading prose. The roadmap **doc** stays canonical for prose (stories, ACs, sequencing
rationale); the register is canonical for execution state.

**LOCAL-ONLY — NEVER COMMITTED (policy lock-in 2026-07-09, owner: Chris Dare).**
Roadmap-run artifacts are per-machine state, not repo content. The register lives under
`.claude/notes/` precisely because `ensure-claude-gitignore.sh` (called by every
pipeline init) excludes that tier in the target repo — via `.git/info/exclude` since
2026-07-09 (round-4 proc-9; never by mutating the tracked `.gitignore`). Do not commit
it, do not move it into a tracked path, do not "back it up" into `plans/`. CI in
claude-mcp-server FAILS if a register is ever found tracked. Cross-machine sharing
happens through the gated GitLab projection (`--gitlab`), never through git.

**Who writes what (single-writer rule — this is the anti-drift discipline):**

| Field class | Single writer | When |
|---|---|---|
| Object creation, `title`, `epic`, `lane`, `depends_on`, `tags`, `rice`, `specialist`, `repos`, `external_writes` | `roadmap-materializer` (Phase 4) **authoring a structural draft that `roadmap-milestones-merge.py` deterministically merges** — the agent never hand-writes the live register | Once, at materialize; re-materialize re-derives structure while the merge preserves execution state |
| `status`, `run.*`, `history[]` | `roadmap-milestones-status.py` invoked by `/milestone-pipeline` (init + completion) | Per milestone run |
| `gitlab.*` | The `/roadmap` orchestrator, after gated `/issue-create` | Only when `--gitlab` was authorized |

No other writer is sanctioned. Hand-edits must pass
`roadmap-milestones-validate.py` before use.

## File shape

```json
{
  "schema_version": 1,
  "slug": "institutional-memory-completion",
  "roadmap_doc": "plans/institutional-memory-completion-roadmap.md",
  "generated_by": "roadmap-materializer",
  "generated_at": "2026-07-09T00:00:00Z",
  "milestones": [
    {
      "id": "institutional-memory-completion-m1",
      "title": "Index agent-memory lesson trees into MCP search",
      "epic": "E1",
      "lane": "now",
      "status": "pending",
      "depends_on": [],
      "tags": ["moscow/must"],
      "rice": 512,
      "specialist": "general-purpose",
      "repos": ["tools/claude-mcp-server"],
      "external_writes": ["git push origin HEAD:main (tools/claude-mcp-server)"],
      "gitlab": {"epic_iid": null, "story_iids": []},
      "run": {
        "state_path": null,
        "started_at": null,
        "completed_at": null,
        "rectification_commit": null,
        "override": null
      },
      "history": []
    }
  ]
}
```

## Field reference

Top level — all five keys REQUIRED, no unknown keys:

| Key | Type | Rule |
|---|---|---|
| `schema_version` | int | Must be `1`. **Writers refuse any other value** (see Versioning below) |
| `slug` | string | `[a-z0-9][a-z0-9-]*`, ≤40 chars; must match the parent directory: `.claude/notes/roadmaps/<slug>/milestones.json` |
| `roadmap_doc` | string | Repo-relative path to the companion roadmap doc |
| `generated_by` / `generated_at` | string | Provenance; `generated_at` is UTC `%Y-%m-%dT%H:%M:%SZ` |
| `milestones` | array | ≥1 object, shape below |

Per milestone — REQUIRED unless noted, no unknown keys:

| Key | Type | Rule |
|---|---|---|
| `id` | string | `<slug>-m<N>`, unique within the file |
| `title` | string | Non-empty; action verb, no conventional-commit prefix |
| `epic` | string | `E<N>` — must be declared in the roadmap doc's `## Epics` |
| `lane` | enum | `now` \| `next` \| `later` |
| `status` | enum | `pending` \| `in_progress` \| `complete` \| `cancelled` |
| `depends_on` | array of ids | Each is a milestone id in this file OR a spike id (`<topic>-spike-N`); no self-reference; the milestone subgraph must be a DAG (spike ids are not graph nodes) |
| `tags` | array of strings | `<namespace>/<value>` kebab-case; exactly one `moscow/*` tag (`must`\|`should`\|`could`\|`wont`) |
| `rice` | number or null | Null for non-Must lanes (Shoulds are ordered by capacity, not RICE) |
| `specialist` | string | Agent name the roadmap assigned (or `general-purpose`) |
| `repos` | array of strings | **Exactly ONE** repo-relative path under the platform tree — a milestone delivers to one repo. A `>1` entry is refused at fresh init (`init-state.sh` exit 6, no override) because the failure is unrecoverable once past `implement-complete`; split into one milestone per repo chained with `depends_on`. See `data/commands/milestone-pipeline.md` § Multi-repo |
| `external_writes` | array of strings | Copied from the roadmap stories' declared external writes |
| `gitlab` | object | `epic_iid` int-or-null, `story_iids` int array. All-null until a gated `--gitlab` run writes back iids |
| `run` | object | `state_path`, `started_at`, `completed_at`, `rectification_commit` (string-or-null each), `override` (object-or-null, see below) |
| `history` | array | Append-only transition log: `{"at": <utc>, "from": <status>, "to": <status>, "reason": <string-or-null>}` |

## Status state machine (enforced by `roadmap-milestones-status.py`)

```
pending ──→ in_progress ──→ complete        (terminal)
   │              │
   └──→ cancelled ←┘                        (reopen: cancelled → pending)
```

- Setting the **current** status again is an idempotent no-op (exit 0) — resume-safe.
- `complete` is terminal. No transition leaves it; a genuinely reopened milestone is a
  new milestone id (`-mNb` or a new roadmap revision) — mirrors the forward-only
  doctrine of `milestone-pipeline-checkpoint.py`.
- **Reopen clears run metadata:** `cancelled → pending` resets `run.*` to nulls (the old
  attempt's `state_path`/timestamps/override would otherwise leak into the fresh run);
  `history[]` keeps the full audit trail.
- **Dependency gate:** `pending → in_progress` REFUSES (exit 3) while any `depends_on`
  entry is unmet. A **milestone** dep is met when its status is `complete`. A **spike** dep
  (id `<topic>-spike-N`) is met ONLY at the spike's ACCEPT terminal
  (`state.json.terminal_status == "accept"`, which itself requires a canonical
  YES/NO/UNCERTAIN verdict) — a `--skip-review` / capped / aborted / not-yet-run spike is
  NOT met, so it cannot silently unblock the milestone. The verdict CONTENT (YES vs NO)
  does not gate: an ACCEPTed spike has ANSWERED the question, and the roadmap author decides
  how the milestone proceeds on that answer. Spike deps resolve **read-only** from the
  spike's local `state.json`; the spike pipeline never writes this register (loose coupling).
  Override: `--override "<reason>"` — allowed, logged into `history[].reason` and
  `run.override`, never silent. Legitimate uses: hotfix out-of-order work, a dependency
  satisfied outside the pipeline. An override is an audit event, not a bypass switch.
- **Concurrency:** all sanctioned writers take an exclusive `fcntl` lock on
  `<register>.lock` around the read-modify-write, then replace atomically — two
  concurrent sessions serialize instead of losing history entries.

## Versioning & migration policy

`schema_version` is frozen at `1`. Writers (`status`, `merge`) REFUSE files with any
other version rather than guess; the validator fails them. A future v2 ships as an
explicit `roadmap-milestones-migrate.py` (one-way, backup-first) plus a validator bump
in the same commit — never as writers that silently tolerate both shapes.

## Relationship to the other artifacts

| Artifact | Relationship |
|---|---|
| `plans/<slug>-roadmap.md` | Prose canon — also a LOCAL run artifact (never committed; same policy). Checkbox status lines (`- [ ]`/`- [/]`/`- [x]`, workspace convention) are a human-readable **view**; `pipeline-reconcile.py` flags checkbox↔status divergence |
| `.claude/notes/milestones/<id>/state.json` | Per-run execution state (gitignored, per-machine). `run.state_path` links to it; reconcile cross-checks phase↔status |
| `docs/<id>_*_critique.md` | Phase 3 outputs (gitignored via the `*_critique.md` patterns); reconcile checks presence + severity counts against `state.json` |
| GitLab issues | Optional projection via gated `--gitlab`; iids write back into `gitlab.*` — the ONLY cross-machine surface |

## Tooling

| Script (flat naming, `data/scripts/`) | Purpose |
|---|---|
| `roadmap-milestones-validate.py <file>` | Structural validity: schema, ID/tag formats, DAG acyclicity, enums. Exit 0 clean / 1 findings / 2 usage. `--self-test` runs embedded fixtures |
| `roadmap-milestones-status.py <file> <id> <status>` | The ONLY sanctioned status writer. Enforces the state machine + dep-gate; `--check-only` dry-runs the gate; locked + atomic; `--field run.<k>=<v>` for run metadata |
| `roadmap-milestones-merge.py <register> <incoming>` | The ONLY sanctioned materialize writer: merges an agent-authored structural draft into the live register, preserving `status`/`run`/`history`/`gitlab` by id; refuses drops of active milestones (exit 3) |
| `pipeline-reconcile.py --repo-root <path>` | Cross-artifact consistency (register ↔ roadmap doc ↔ state.json ↔ critiques). Advisory exit 1 on findings |

Content policy (MoSCoW must-cap, RICE ranking) deliberately stays with
`roadmap-score-moscow.py` / `roadmap-score-rice.py` at sequence time — the validator
checks **structure**, not planning judgment. One authority per check.

## Won'ts (design locks, v1)

- **Never committed to git** — see the policy block at the top. The register is not repo
  content; there is no "tracked mode".
- **No auto-creation of GitLab issues from this file** — it is the local regulated
  register; projection to GitLab stays behind the existing `--gitlab` gate.
- **No YAML.** Engineer machines run stdlib python3 (no PyYAML outside CI); JSON keeps
  every consumer dependency-free.
- **No stories/ACs duplicated into the JSON** — prose lives once, in the roadmap doc.
- **No agentic enforcement of these invariants** — deterministic scripts block;
  agents (critics/challengers) advise. Reserve model judgment for content quality.
- **Non-spike external preconditions stay in the roadmap doc.** `depends_on` accepts
  milestone ids AND spike ids (`<topic>-spike-N`; see the Dependency gate — added 2026-07-10,
  Tier 3). But a precondition that is neither — an external event, a manual approval, a
  third-party dependency — lives as a `[MUST]` assumption with a Validation clause in the
  roadmap doc, enforced by `roadmap-validate.py`, not encoded here.
