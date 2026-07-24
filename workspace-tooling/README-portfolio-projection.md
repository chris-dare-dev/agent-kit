# Milestone portfolio projection

`portfolio_projection.py` creates a deterministic Obsidian-facing view over the existing roadmap
documents, roadmap registers, and milestone pipeline state. Those source artifacts remain
authoritative; generated files are one-way projections and must not be used to mutate status.

## Why this exists

The existing status surfaces overload three different claims:

1. a roadmap/checklist item was checked;
2. code or documentation passed its implementation pipeline;
3. a declared revision was applied and independently verified on every required target.

The projection keeps those axes separate. Legacy checkbox `done` and v1 pipeline `complete` stay
in `tracking_status`; they never imply `implementation_status=published` or
`operational_status=verified`. Until v2 evidence exists, the latter fields are `unknown` and the
record enters the `tracking-complete-delivery-unknown` attention queue.

## Outputs

- `Notes/Portfolio/portfolio-index.json` — normalized machine-readable artifact, validated by
  `scripts/schemas/portfolio-index.schema.json`.
- `Notes/Portfolio/Milestones/*.md` — flat-property generated records for Obsidian Bases.
- `Notes/Bases/Milestone Portfolio.base` — attention queues and portfolio tables.
- `Notes/Portfolio/Portfolio.md` — human landing page and embedded Base.

Generated notes intentionally contain no task checkboxes, so the Tasks plugin does not count a
second copy of roadmap work. Each note carries a per-record content hash rather than the global
portfolio hash, so one source-state change does not force Obsidian to re-index every record.

## Usage

```bash
python3 scripts/portfolio_projection.py
python3 scripts/portfolio_projection.py --check
python3 scripts/portfolio_projection.py --check --json-summary
```

`--check` is read-only and exits nonzero if generated output is missing, stale, or changed. The
default mode writes only generated paths and prunes only stale Markdown records inside the owned
`Notes/Portfolio/Milestones/` directory.

## Migration behavior

- v1 roadmap registers are labeled `roadmap-register-v1` and retain their own tracking status.
- Markdown-only roadmaps are labeled `legacy-markdown`; their parsed checkbox status is retained.
- v2 register fields (`record_role`, `canonical_id`, `canonical_owner`, `implementation_status`,
  `operational_status`, and `required_targets`) are projected directly when they appear.
- Missing or conflicting ownership is `unassigned`, not guessed.
- A roadmap document backed by a register is emitted once from the register, not duplicated from
  its Markdown checkboxes.

The next pipeline phase should add the v2 implementation, release, operations-plan, operations-
evidence, and waiver artifacts described in `plans/milestone-pipeline-delivery-state-v2.md`.
