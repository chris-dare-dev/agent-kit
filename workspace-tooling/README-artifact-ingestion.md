# Phase 2–7 artifact lifecycle

`artifact_ingestion.py` turns the Phase 1 artifact catalog into verified,
replayable ingestion units for Qdrant and Graphiti:

```text
canonical workspace files
  -> Phase 1 SQLite catalog
  -> immutable, namespaced gzip JSONL outbox
  -> Qdrant semantic chunks
  -> Graphiti temporal episodes on FalkorDB

successful artifact-producing skills
  -> Phase 5 append-only skill-event receipts
  -> Phase 7 verified incremental receipt consumer
  -> deterministic per-event Qdrant outboxes

terminal artifact candidates
  -> Phase 6 conservative classification
  -> immutable external quarantine set
  -> optional acknowledged relocation + restore
```

This is a derived-data pipeline, not a new source of truth. No command moves,
rewrites, links, or deletes a canonical file during catalog, ingestion, skill
capture, or quarantine staging. The separate Phase 6 quarantine apply can
relocate a sealed source by same-filesystem rename after a second
acknowledgement and a fresh safety preflight. No command deletes source files,
Qdrant points, Graphiti data, checkpoints, old outboxes, or quarantine sets.

## Runtime

The `prepare` and plan paths use the standard library. Live clients require
Python 3.12 and the pinned packages in
`requirements-artifact-ingestion.txt`. Keep the environment outside the
Obsidian/workspace tree:

```sh
DERIVED="$HOME/.local/share/workspace-artifacts"
python3.12 -m venv "$DERIVED/venv"
"$DERIVED/venv/bin/python" -m pip install \
  -r scripts/requirements-artifact-ingestion.txt
```

Current pins are Qdrant Client 1.16.1 with FastEmbed and Graphiti Core 0.29.2
with FalkorDB support. Graphiti also installs a Neo4j Python client as a
transitive package; this pipeline never creates or connects to a Neo4j
service. FalkorDB is the only permitted graph backend here.

## 1. Refresh the source inventory

Run Phase 1 immediately before preparing an outbox:

```sh
python3 scripts/artifact_catalog.py --json-summary
```

The catalog records exact content hashes. If a source changes after the scan,
outbox preparation stops instead of ingesting bytes that do not match the
catalog revision.

## 2. Prepare an immutable v2 outbox

```sh
python3 scripts/artifact_ingestion.py prepare
```

The default location is:

```text
~/.local/share/workspace-artifacts/outbox/catalog-run-<run>-chunks-v2/
├── ingest-units.jsonl.gz
└── manifest.json
```

Preparation:

- resolves every source back inside `$HOME/Work/workspace`;
- opens regular files without following final-component symlinks;
- verifies the SHA-256 from the Phase 1 revision;
- creates deterministic Markdown-aware chunks;
- adds overlap only to Qdrant embedding text, not Graphiti episode content;
- assigns deterministic unit and Qdrant point UUIDs;
- assigns each Graphiti unit a deterministic repository/project namespace;
- writes the manifest last, after flushing the compressed stream.

Consumers require `complete: true` and verify the uncompressed JSONL checksum.
An interrupted preparation remains manifest-less and cannot be consumed.
Existing outboxes are never overwritten.

The original Phase 2 run 4 produced 35,350 units from 7,594 non-empty
artifacts. That 47 MB v1 outbox is retained as immutable evidence:

```text
~/.local/share/workspace-artifacts/outbox/catalog-run-4-chunks-v1
```

V1 remains valid input for Qdrant replay. Graphiti apply from v1 is now
disabled because its single global namespace allowed unrelated repositories
to merge entities. New outboxes use schema v2 and a namespace of the form
`workspace_artifacts_<scope>_<digest>` for every unit. The manifest records the
shared `graphiti_group_prefix`.

## 3. Qdrant

The default is persistent embedded Qdrant storage at:

```text
~/.local/share/workspace-artifacts/qdrant
```

Plan a stable chronological prefix:

```sh
V="$HOME/.local/share/workspace-artifacts/venv/bin/python"
OUTBOX="$HOME/.local/share/workspace-artifacts/outbox/catalog-run-7-chunks-v2"

"$V" scripts/artifact_ingestion.py qdrant \
  --outbox "$OUTBOX" \
  --limit-units 2000
```

Apply that prefix explicitly:

```sh
"$V" scripts/artifact_ingestion.py qdrant \
  --outbox "$OUTBOX" \
  --limit-units 2000 \
  --batch-size 32 \
  --apply
```

`--limit-units N` always means the first `N` chronological outbox units, not
the next `N` pending units. Repeating the same command is therefore a real
replay test. Qdrant point IDs are deterministic, writes use upsert, and the
SQLite checkpoint skips completed units.

Catalog run 9 completed the first full-corpus apply on 2026-07-17: 35,444
points from 7,616 non-empty current artifacts, with 0 failed writes. The first
2,000-point Phase 3 prefix was recognized as already complete, so the full
apply embedded only the remaining 33,444 units. Reconciliation scanned all
35,444 points and found every one current, with no lifecycle payload changes
required. A concurrent roadmap receipt then advanced the catalog. The Phase 7
run 10 operational snapshot contains 7,623 current artifacts and 35,449
full-corpus units. After its incremental event and additive history retention,
Qdrant contained
36,105 points: 35,459 current and 646 historical. The ten extra current points
are the receipt consumer's 4,000-character event chunks for the same current
artifacts; search remains revision-filtered.

Search current revisions:

```sh
"$V" scripts/artifact_ingestion.py search \
  --query "Mosaic telemetry architecture metrics logs traces Grafana Loki Tempo"
```

Use `--include-history` when superseded catalog revisions should be eligible.
Qdrant retains points for ingested historical revisions; the default search
uses a `catalog_current = true` payload filter and checks result revision IDs
against the current Phase 1 catalog. Exact payload filters can be combined:

```sh
"$V" scripts/artifact_ingestion.py search \
  --query "deployment decision" \
  --project platform \
  --artifact-type decision \
  --authority-class authoritative \
  --repository platform \
  --lifecycle-hint active
```

Available exact filters are `--project`, `--artifact-type`,
`--authority-class`, `--repository`, and `--lifecycle-hint`.

After refreshing the catalog or ingesting another prefix, reconcile the
additive lifecycle payload:

```sh
"$V" scripts/artifact_ingestion.py qdrant-reconcile
"$V" scripts/artifact_ingestion.py qdrant-reconcile --apply
```

Reconciliation only sets `catalog_current` to `true` or `false`; it never
deletes historical points. It refuses to mutate a collection if required
revision payloads are malformed. It also requests keyword/bool payload indexes
for the filter fields. Embedded Qdrant accepts those requests but warns that
indexes have no performance effect in local mode; use Qdrant server before
depending on indexed-filter performance at larger scale.

For a server rather than embedded storage, replace the default path with:

```sh
--qdrant-url http://127.0.0.1:6333
```

`QDRANT_API_KEY` is read only when present. Do not point local storage inside
the workspace.

## 4. Graphiti on FalkorDB

Plan mode requires neither a running database nor model credentials. By
default, only `decision`, `handoff`, `plan`, and `roadmap` artifacts are
eligible. Repeat `--artifact-type` to override that route:

```sh
"$V" scripts/artifact_ingestion.py graphiti \
  --outbox "$OUTBOX" \
  --limit-units 10
```

A live apply is intentionally gated. It requires all of the following:

1. A FalkorDB endpoint reachable on the selected host and port.
2. An OpenAI-compatible LLM that has passed a small Graphiti structured-output
   extraction test.
3. An OpenAI-compatible embedding endpoint plus its exact model name and
   vector dimension.
4. The hard quality-pilot limit `--limit-units 1`.
5. A namespaced v2 outbox.
6. `--apply`.

### Local pilot service

The Phase 3 local pilot uses FalkorDB 4.20.1 bound only to loopback, with AOF
persistence in the derived-data tree:

```sh
docker-compose \
  -f "$HOME/.local/share/workspace-artifacts/services/falkordb/compose.yaml" \
  up -d
```

The pilot intentionally has no password and must not be exposed beyond
`127.0.0.1:6379`. Its browser port is not published. The pinned FalkorDB
container is distributed under SSPLv1; any shared, hosted, or production use
requires an explicit licensing/legal review rather than promotion of this
local compose file.

Check the database and model pair without creating a graph:

```sh
"$V" scripts/artifact_ingestion.py graphiti-check \
  --llm-model qwen3:8b \
  --embedding-model nomic-embed-text \
  --embedding-dim 768 \
  --probe-models
```

This check lists graphs, verifies strict structured output, and verifies the
embedding dimension, finiteness, and norm. It does not initialize Graphiti
telemetry, create indexes, or write an episode.

`qwen3:8b` plus `nomic-embed-text` passed this mechanical readiness check on
2026-07-17, but that does **not** approve the LLM for bulk extraction. Quality
must pass an isolated episode review first.

After validating a model pair and reviewing a plan, use a deliberately small
pilot:

```sh
export GRAPHITI_LLM_API_KEY='<provider key>'
export FALKORDB_PASSWORD='<password if configured>'

"$V" scripts/artifact_ingestion.py graphiti \
  --outbox "$OUTBOX" \
  --host 127.0.0.1 \
  --port 6379 \
  --database workspace_artifacts \
  --group-prefix workspace_artifacts \
  --llm-model qwen3:8b \
  --embedding-model nomic-embed-text \
  --embedding-dim 768 \
  --structured-output-mode json_schema \
  --limit-units 1 \
  --apply
```

FalkorDB stores each v2 group ID as a separate graph/database derived from the
repository/project scope. The requested group prefix must match the manifest.
The adapter switches to the target graph, initializes its indexes, and
validates that the returned episode, nodes, and edges all carry that group ID
before marking the checkpoint complete. Telemetry is disabled before Graphiti
is initialized, and extraction concurrency is one. Episodes are processed
sequentially in chronological eligible-unit order.
Default extraction instructions limit facts to explicit decisions, status,
ownership, dependencies, milestones, deadlines, and supersession; they also
forbid invented generic relationships and incidental config-key entities.

Before each episode, the pipeline commits an `in_progress` write-ahead
checkpoint. If the API call raises or the process dies after that checkpoint,
the unit becomes `ambiguous` or remains `in_progress`; neither state is retried
automatically. Inspect FalkorDB first, then use `--retry-ambiguous` only after
deciding that another attempt is safe.

### Preserved v1 quality evidence

Two v1 episodes were written to the original global `workspace_artifacts` group
during the local quality pilot. They are intentionally preserved. The second
episode caused facts from an SES/Keycloak handoff to attach to a
`DNS_HOSTED_ZONE_ID` entity introduced by an unrelated Mosaic document. This
demonstrated that a global entity namespace is unsafe even when the model and
database are mechanically ready.

Those two episodes are evidence only. Do not extend the v1 group.

The first v2 episode proved that the repository namespace prevents the prior
Mosaic/Keycloak collision: its 21 nodes exist only in the Keycloak graph.
However, `qwen3:8b` still failed the extraction-quality gate. It created
incidental file/config entities and emitted four unresolved-edge warnings.
Its 10 persisted facts did have meaningful semantic names such as `USES`,
`REQUIRES`, and `DEPENDS_ON`; FalkorDB's physical relationship type is always
`RELATES_TO`, while Graphiti stores the semantic type in the relationship's
`name` property. The episode remains preserved as quality evidence. The
adapter therefore hard-caps applies at one episode; bulk Graphiti ingestion
remains disabled until a stronger model passes the same direct inspection.

### Phase 4 controlled Graphiti pilot

`graphiti_pilot.py` qualifies a model against three fixed units from the
immutable catalog-run-7 v2 outbox: one decision, one handoff, and one roadmap.
It creates a separate model-, version-, and case-specific FalkorDB graph for
each unit, audits the graph after every episode, and stops on the first
failure. It never deletes a graph, retries a partial namespace, mutates a
source document, or enables the bulk adapter.

```sh
"$V" scripts/graphiti_pilot.py plan --model qwen3:14b

"$V" scripts/graphiti_pilot.py run \
  --model qwen3:14b \
  --reasoning-effort none \
  --request-timeout-seconds 180 \
  --case-timeout-seconds 600 \
  --apply
```

The pilot requires exactly one episode, 2–18 typed entities, 1–16 facts,
episode provenance and `valid_at` on every fact, exact namespace containment,
no unresolved-edge warnings, no generic relationships, no incidental
file/config/code entities, only the controlled semantic vocabulary, and at
least two expected corpus terms. A model must pass all three cases before it
can be considered for a separate bulk-unlock decision.

The preserved `qwen3:14b` results are:

- **v1 operational failure:** provider-default Qwen3 thinking remained in
  attribute extraction for more than 20 minutes. The attempt was interrupted
  before persistence; its namespace contains zero nodes. Its immutable report
  is
  `~/.local/share/workspace-artifacts/graphiti-pilots/qwen3_14b/pilot-v1-report.json`.
- **v2 quality failure:** `reasoning_effort=none` reduced the decision case to
  67 seconds and persisted one correctly namespaced episode with 9 entities
  and 8 facts. Temporal fields, provenance, required-term recall, typing, and
  namespace isolation passed. The graph still included code symbols
  (`matchesKind` and `writescope.Check()`) and semantic relations outside the
  controlled vocabulary (`Extends` and `Rejects`). The run stopped before the
  handoff and roadmap cases. Its immutable report is
  `~/.local/share/workspace-artifacts/graphiti-pilots/qwen3_14b/pilot-v2-report.json`.
- **v3 pre-persistence rejection:** the new exact-object entity guard rejected
  `writescope.Check()` and `matchesKind` in 18 seconds, before Graphiti could
  resolve or save them. The fresh v3 decision namespace contains 0 episodes,
  0 entities, and 0 facts; the handoff and roadmap cases were not started.
  The immutable report is
  `~/.local/share/workspace-artifacts/graphiti-pilots/qwen3_14b/pilot-v3-report.json`.

`graphiti_policy.py` now wraps the exact extracted nodes and edges used by
`Graphiti.add_episode`. It blocks incidental path/config/code/hash entities,
untyped or wrong-namespace nodes, generic or unapproved edge names, missing
episode provenance, missing `valid_at`, and count-limit violations before the
objects reach persistence. Prompting is still only guidance; this policy is
the enforcement boundary. The v3 result proves the boundary works but does
not approve qwen3:14b. Bulk Graphiti remains disabled until one model passes
all three fresh cases. All prior namespaces and reports remain evidence; do
not delete or extend them.

## 5. Skill integration and incremental capture

`artifact_skill_capture.py` gives the handoff, roadmap, spike, and
milestone-pipeline skills one shared finalization hook. It records a
content-addressed event only after each skill's successful terminal gate:

| producer | captured set | successful terminal |
|---|---|---|
| `handoff` | the continuation/review files written in the run | validation complete |
| `roadmap` | roadmap Markdown + machine milestone register | materializer complete |
| `spike` | policy-eligible documents under the spike evidence directory | `complete` or audited `skip-review` |
| `milestone-pipeline` | policy-eligible documents under the milestone evidence directory | checkpoint `complete` |

An emit is a receipt, not a sink write. It hashes a stable no-follow snapshot,
reuses the Phase 1 type/authority/source metadata and IDs, marks Qdrant
eligibility, and marks only decisions/handoffs/plans/roadmaps as Graphiti
candidates. Every event also records `qdrant_write=disabled`,
`graphiti_write=disabled`, and `graphiti_bulk=disabled`.

Plan a handoff event without creating anything:

```sh
python3 scripts/artifact_skill_capture.py emit \
  --workspace "$HOME/Work/workspace" \
  --producer handoff \
  --run-id "example-2026-07-17-continuation" \
  --path plans/HANDOFF-example-continuation.md
```

The skills add `--apply` after their own terminal gate. Apply creates exactly
one exclusive `0600` JSON receipt:

```text
~/.local/share/workspace-artifacts/skill-events/<event-prefix>/<event-sha256>.json
```

The event ID is deterministic over producer, stable run ID, and the sorted
artifact path/revision pairs. Repeating an identical capture returns
`idempotent`; changed content creates a new event while the old receipt stays
in place. Receipt bytes are fsynced under a private temporary name and
published atomically with no-replace semantics; a crash exposes either no
receipt or the complete receipt. Existing receipts are never overwritten. The tool rejects source
or output paths that escape their trust boundaries, rejects explicit
or intermediate user-controlled symlinks, enforces owner-only `0700`/`0600`
derived-state permissions, the default-deny catalog policy, and the
producer/type contract, and never includes artifact content in the receipt.

Receipt capture is deliberately best-effort after skill completion. A failure
cannot roll back a finalized skill artifact, so the skill reports
`unavailable` or `failed` and leaves both the source and prior receipts
unchanged. Failed, aborted, retry, and reconsideration terminals do not emit.

`artifact_event_consumer.py` now consumes these events. It revalidates the
receipt schema, filename/shard/event digest, producer/type contract, safety
flags, current catalog metadata, canonical no-symlink source path, and source
hash before atomically publishing one immutable event outbox. Malformed,
unsafe, or incomplete inputs are recorded in a durable dead-letter audit and
do not prevent unrelated valid events from completing. It idempotently upserts the
Qdrant chunks and records Graphiti candidate counts without making a Graphiti
write. A stale receipt is recorded terminally stale; a failed sink attempt can
be retried from its checkpoints. Failed reconciliation is scheduled
independently and retried even when no new receipt arrives. Receipt presence
never grants Graphiti approval.

Plan without creating state or outboxes:

```sh
python3 scripts/artifact_event_consumer.py consume
```

Apply pending receipts and refresh the catalog once when needed:

```sh
"$V" scripts/artifact_event_consumer.py consume \
  --refresh-catalog \
  --apply
```

Status is read-only:

```sh
python3 scripts/artifact_event_consumer.py status
```

Inspect or disposition poison inputs without changing their bytes:

```sh
python3 scripts/artifact_event_consumer.py dead-letter-list --open-only
python3 scripts/artifact_event_consumer.py dead-letter replay \
  --id 'dead:<sha256>' --resolution 'canonical receipt restored' --apply
python3 scripts/artifact_event_consumer.py dead-letter resolve \
  --id 'dead:<sha256>' --resolution 'operator disposition' --apply
```

The consumer state is
`~/.local/share/workspace-artifacts/artifact-event-consumer.sqlite3`; per-event
outboxes are named `skill-event-<sha256>-chunks-v2`. Neither is stored in the
workspace. The catalog records complete, degraded, and failed scan attempts;
only the latest complete generation can become authoritative for retrieval,
ingestion, or quarantine.

## 6. Quarantine before cleanup

`artifact_quarantine.py` is the reversible Phase 6 boundary. It does not infer
that every document containing words such as `superseded` or `closed` is safe
to move. It classifies only policy-scoped plans and repository plan folders:

- **eligible** requires a decision, handoff, plan, research artifact, or
  roadmap with a terminal path marker, exact frontmatter status, or explicit
  opening banner; a minimum age of 14 days; and no active, canonical,
  review-open, or Git-tracked signal;
- **review-only** includes broad catalog lifecycle hints and `closed` without
  stronger terminal evidence;
- **blocked** includes young, active, canonical, Git-tracked, conflicting,
  unreadable, or source-drifted candidates.

The default commands are read-only plans:

```sh
python3 scripts/artifact_quarantine.py plan
python3 scripts/artifact_quarantine.py stage
```

After reviewing the complete plan with `--details`, seal the exact assessment:

```sh
python3 scripts/artifact_quarantine.py stage --apply
```

The sealed set lives outside the workspace:

```text
~/.local/share/workspace-artifacts/quarantine/sets/<set-sha256>/
├── files/          # verified 0400 copies of eligible artifacts only
└── manifest.json   # written last; eligible, review, and blocked queues
```

The set ID authenticates the catalog run, workspace, rules, source hashes, and
selection records. Repeating the same stage is idempotent. A review-only set
is still useful evidence and contains no copied artifact bodies.

Relocation is deliberately a second operation. First inspect it:

```sh
SET='quarantine-set:<set-sha256>'
python3 scripts/artifact_quarantine.py status --set "$SET"
python3 scripts/artifact_quarantine.py quarantine --set "$SET"
```

An apply requires the exact set ID twice:

```sh
python3 scripts/artifact_quarantine.py quarantine \
  --set "$SET" \
  --acknowledge "$SET" \
  --apply
```

Before moving anything, the tool re-verifies every staged and source hash and
fails the whole preflight if it finds Git tracking, source drift, a live
Obsidian/project symlink alias, a literal catalog backlink, an existing
destination, or a cross-filesystem boundary. An accepted artifact is renamed
under the sealed set's `relocated/` tree; it is never unlinked. Operations are
recorded in a fsynced append-only `events.jsonl`, and an interrupted rename can
be recovered on a rerun.

Restore is symmetric and also defaults to a plan:

```sh
python3 scripts/artifact_quarantine.py restore --set "$SET"
python3 scripts/artifact_quarantine.py restore \
  --set "$SET" \
  --acknowledge "$SET" \
  --apply
```

There is intentionally no `cleanup`, `delete`, or expiry command. Any future
destructive cleanup is a separate human-approved phase after an observation
window and a demonstrated restore; Phase 6 does not authorize it. Tracked
documents must be retired through their normal Git workflow, not by this
quarantine tool. Dependency trees such as `.venv` are outside the artifact
policy and require a separate explicit review.

The first sealed production assessment used catalog run 8 on 2026-07-17:

```text
set:      quarantine-set:22e0d02c2de4cd2ac182654b487cfc5e0ff35ec106ebbc50fb9e7ec7a5cacef9
eligible: 0
review:   276
blocked:  7
moved:    0
deleted:  0
```

That zero-move result is intentional: broad lifecycle prose is not sufficient
authority to relocate a source document.

## 7. Daily access through the MCP server

Agents and MCP-capable clients should use the existing
`@chris-dare-dev/agent-kit` rather than opening Qdrant or FalkorDB directly:

- `artifact_memory_status` — catalog, Qdrant, Graphiti, and receipt health;
- `search_artifacts` — semantic Qdrant discovery, current revisions by default;
- `get_artifact` — canonical source retrieval with hash/size/mtime/symlink
  verification;
- `query_temporal_facts` — approved Graphiti groups only; unapproved pilot
  access is hard-disabled until publication and approval controls exist.

The local Python equivalent is:

```sh
"$V" scripts/artifact_memory.py status
"$V" scripts/artifact_memory.py search --query "milestone ownership"
"$V" scripts/artifact_memory.py get --relative-path "plans/example.md"
"$V" scripts/artifact_memory.py facts --query "ownership changed"
```

Qdrant output is a discovery hint; retrieve the canonical source before
acting. Historical Qdrant snippets are hard-disabled until immutable
CAS-backed revision retrieval exists. Graphiti has no approved groups on this
machine today, and `--include-pilot` is rejected rather than treated as an
approval. Under `SERVER_PROFILE=shared`, all four artifact-memory tools are
absent from discovery and rejected at dispatch.

Codex is registered through `~/.codex/config.toml`; Claude Code uses the
workspace/platform MCP registration. Restart the client after changing MCP
configuration or rebuilding the server.

The launchd job `com.workspace.artifact-event-consumer` checks for new receipts
every 15 minutes and at login. Its canonical plist is
`scripts/com.workspace.artifact-event-consumer.plist`; owner-only stdout and
stderr logs go to
`~/Library/Logs/workspace-artifact-event-consumer.out.log` and
`~/Library/Logs/workspace-artifact-event-consumer.error.log`. Each monitored run
also atomically updates the owner-only
`~/.local/share/workspace-artifacts/artifact-event-consumer-health.json` file and
emits a deduplicated local notification when health first becomes degraded.

## 8. Checkpoints and recovery

```sh
"$V" scripts/artifact_ingestion.py status
```

The default state database is:

```text
~/.local/share/workspace-artifacts/ingestion-state.sqlite3
```

Checkpoints are scoped by sink, location, collection/database, and model
configuration. Changing an embedding model creates a different target
identity. Qdrant also rejects attempts to mix vector dimensions or a different
embedding-model payload into an existing collection.

Safe recovery rules:

- stale catalog revision: refresh the Phase 1 catalog and create a new outbox;
- incomplete legacy outbox: isolate it outside the published outbox namespace
  with an audit record; current writers publish complete directories
  atomically;
- Qdrant failed batch: rerun the same stable prefix;
- receipt consumer failure: inspect the health projection, consumer status,
  and dead-letter list, then rerun the same event; immutable outboxes and
  Qdrant IDs make this idempotent;
- Graphiti ambiguous episode: inspect the graph before explicit retry;
- model change: use a new collection/target rather than mixing embeddings.

## 9. Composite watermark and the external watchdog

Every component of this system reports its own health, and on 2026-07-18 every
one of those surfaces was green while the pipeline had been dead for roughly
fourteen hours: the bootstrap health file proved only that `docker compose up`
succeeded, the consumer health file was thirteen hours stale and still said
`healthy: true`, and the only alert channel was a desktop notification fired
*from the consumer itself* — so the one failure that mattered most was the one
it structurally could not report.

Two surfaces close that gap.

**Composite watermark** — one system-level answer, not eleven component-level
ones:

```sh
python3 scripts/artifact_watermark.py          # human summary, exit 1 when unhealthy
python3 scripts/artifact_watermark.py --json   # stable JSON document
```

It reports, per stage: catalog run age, oldest *unobserved* receipt age (the
direct measure of the ADR-002 artifact-to-search SLO), consumer last-success
time **and** launchd loaded-state, the active writer sink identity versus the
serving generation, service/bootstrap/watchdog health-file freshness, snapshot
age, outbox-quarantine count, the cached Obsidian validator verdict, and the
graphiti-write-disabled assertion.

Two rules distinguish it from the per-component files:

- **Max age.** A health file older than its own expected cadence is `stale`,
  and a stale file never contributes its self-reported verdict. Its claim is
  still shown as `reported`, so the contradiction is visible rather than
  silently resolved in the file's favour.
- **Gaps are owned.** A stage nobody observed is `unknown` — an issue code,
  not a pass.

**External watchdog** — the alert channel that is not the patient:

```sh
python3 scripts/artifact_watchdog.py --json
```

It asserts that the consumer LaunchAgent is *loaded* and that it *ran* within
its cadence, then writes to two durable channels: an append-only JSONL log at
`~/Library/Logs/workspace-artifact-watchdog.jsonl` and a desktop notification
raised by the watchdog process. Notifications fire on state transitions so a
persistent outage does not become a notification storm; `--force-notify`
overrides that, and the log records every run either way.

It runs from two independent places:

- the Qdrant bootstrap job (`com.workspace.artifact-qdrant-bootstrap`, every
  300 s) invokes it on every run, including runs where the Docker phase
  failed — a Qdrant outage must not silence consumer alerting, and a watchdog
  fault never changes the bootstrap job's own exit status;
- `scripts/com.workspace.artifact-watchdog.plist` is a standalone sibling agent
  for the same check. It is authored but **not loaded** — loading it is a
  launchd mutation for Chris to make:

  ```sh
  cp scripts/com.workspace.artifact-watchdog.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.workspace.artifact-watchdog.plist
  ```

The watchdog runs on `/usr/bin/python3` by design: it cannot depend on the
same virtualenv whose absence it may need to report.

`artifact_event_consumer.py status` now also reports `outbox_quarantine`
alongside `dead_letters`. These are two different objects that shared one
name — a dead-letter *record* in the consumer database versus a quarantined
outbox *directory* on disk — and only the first was ever surfaced, so an
operator auditing dead letters could read `total: 0` while a quarantined
outbox sat in `outbox-dead-letter/`. A non-empty quarantine now also makes
`status` exit non-zero.

## 10. Supervisor state-change receipts

The watermark reports the consumer's launchd loaded-state *now*. It cannot say
when that state last changed, or why. On 2026-07-18 that gap cost us the
answer to a direct question: the receipt consumer left `launchctl` sometime
around 00:57Z and **no artifact records when or why**. The last consumer output
and the plist mtime bracket the window; the transition and its intent are
unrecoverable. Given the write-target defect the unload may even have been the
correct protective act — nothing says so.

Every load, unload, bootstrap, or bootout of a `com.workspace.artifact-*` agent
therefore gets an append-only receipt:

```sh
python3 scripts/artifact_supervisor_receipt.py emit \
  --label com.workspace.artifact-event-consumer \
  --action unload \
  --reason "quarantined pending write-target review" \
  --plist scripts/com.workspace.artifact-event-consumer.plist          # plan only
```

Add `--apply` to write it. Receipts land under
`~/.local/share/workspace-artifacts/supervisor-events/`, content-addressed and
0600, published by the same atomic no-replace link as skill-capture receipts —
an existing receipt is never overwritten.

Three properties are deliberate:

- **The observation instant is part of the event identity.** Two loads of one
  agent at different times are two facts, not one. Re-running the same command
  with the same `--observed-at` is idempotent; running it an hour later is a
  new receipt. This is the one way supervisor receipts differ from
  skill-capture receipts, which are content-addressed on the artifact set
  alone.
- **`--reason` is required and must state intent.** A receipt recording only
  that a state change happened would reproduce the F-18 gap with more
  ceremony. Blank and filler reasons are rejected.
- **The tool records; it never mutates.** It runs the `launchctl` command for
  you at no point. It does probe `launchctl print` read-only, so the receipt
  captures the state the system was actually in — which is what distinguishes
  "the operator unloaded it" from "it was already gone".

Because recording is decoupled from acting, the pairing is a runbook
discipline, not an enforced invariant: **emit the receipt in the same step as
the `launchctl` command.** The `launchctl bootstrap` in §9 is exactly such a
step —

```sh
cp scripts/com.workspace.artifact-watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.workspace.artifact-watchdog.plist
python3 scripts/artifact_supervisor_receipt.py emit --apply \
  --label com.workspace.artifact-watchdog --action bootstrap \
  --reason "enable standalone watchdog sibling agent" \
  --plist scripts/com.workspace.artifact-watchdog.plist
```

Replay the timeline the F-18 window was missing:

```sh
python3 scripts/artifact_supervisor_receipt.py history
python3 scripts/artifact_supervisor_receipt.py history --label com.workspace.artifact-event-consumer
```

The history is empty until the first state change is recorded. It is not
backfilled: the 00:57Z unload stays unrecoverable, and the residue ledger
records it as such rather than inventing a receipt for it.

## Current boundary

Phase 7 completes the local access loop: the full current corpus is searchable
in Qdrant, skill receipts have a scheduled fail-closed incremental consumer,
canonical sources and approved facts are exposed read-only through MCP, and
the Graphiti path has an exact pre-persistence policy guard. The first sealed
quarantine assessment still moved nothing: 276 documents need human review
and 7 are blocked. The system does not permit bulk Graphiti writes, automate
source deletion, expire lifecycle history, remove Qdrant points, clean
graphs, or approve a model.
