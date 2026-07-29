# Read-only artifact catalog

`artifact_catalog.py` inventories the workspace's canonical agent-artifact roots
without writing to, moving, linking, or deleting source files. It is the first
step toward a Qdrant/Graphiti pipeline: SQLite is the source inventory and its
`blob_uri` column is intentionally empty until a later, explicitly authorized
archive phase.

The scan is default-deny. The supplied policy admits top-level Markdown plus
documents under `plans`, `docs`, `.claude/notes`, and
`.claude/agent-memory` at workspace and repository scope. It rejects arbitrary
repository YAML/JSON/logs and excludes `Vault`, `Notes`, worktrees, Git
metadata, dependency/build trees, `.aggregate`/`run.failed` output, vendored
CUE packages, and `ai-studio`. Symlinks are skipped so vault aliases cannot
become canonical records.

Run a safe inventory first:

```sh
python3 scripts/artifact_catalog.py --dry-run --json-summary
```

The default derived output is outside the vault at
`~/.local/share/agent-kit/`:

```sh
python3 scripts/artifact_catalog.py --json-summary
```

That atomically replaces `artifact-catalog.sqlite3` and
`artifact-catalog-summary.json`. Existing database history is copied into a
staging database before a new scan row is added, then the staged database is
atomically renamed into place. A lock prevents concurrent derived-output
writes. `--dry-run` creates neither file. The output directory is rejected if
it resolves anywhere inside the source workspace.

Use an alternative derived-output directory for experiments:

```sh
python3 scripts/artifact_catalog.py \
  --workspace $HOME/Work/workspace \
  --policy scripts/artifact-policy.json \
  --output-dir /tmp/agent-kit-catalog \
  --json-summary
```

The SQLite database contains `scan_runs`, stable logical `artifacts`, immutable
content-addressed `artifact_revisions`, and `artifact_observations`. Query
`duplicate_content` for duplicates in the latest completed scan, or
`duplicate_content_history` across every retained revision.
`artifact_id` is derived from the workspace-relative canonical path;
`revision_id` combines that stable ID with the SHA-256 of the file bytes.

Phases 2–4 consume the catalog through a verified immutable, namespaced
outbox. See
[`README-artifact-ingestion.md`](README-artifact-ingestion.md) for the
Qdrant lifecycle/filter pipeline, FalkorDB-only Graphiti adapter,
repository/project namespace boundary, checkpoint behavior, controlled
Graphiti model qualification, and live activation gates.
