---
project: agent-kit
type: doc
authorship: agent-generated
tags:
- project/agent-kit
- type/doc
- authorship/agent-generated
---

# Spike: what does keeping the Graphiti pilot actually cost?

- **Issue:** [#14](https://github.com/chris-dare-dev/agent-kit/issues/14), feeding the owner decision [#15](https://github.com/chris-dare-dev/agent-kit/issues/15)
- **Date:** 2026-07-29 · **Time-box:** 2 days · **Actual:** ~2 hours
- **Output:** this memo. No production code was changed by this spike.

## The question

`query_temporal_facts` is 1 of the 17 advertised MCP tools and the temporal-graph
leg of goal D ("memory management via Obsidian + Qdrant + Graphiti"). Before the
owner can decide keep-or-retire, the "keep" branch has to be priced honestly —
because it points a graph database and an LLM at a project whose other two
stated goals are *runs on any OS* and *nothing leaves the machine*.

## 1. What is actually there today

**The tool cannot return data. Not "does not", *cannot*.**

`_approved_groups()` (`workspace-tooling/artifact_memory.py:284-298`) reads
`graphiti-approvals.json`. That file is read in four places
(`artifact_memory.py:35`, `:321`, `:621`, `artifact_memory_service.py:1494`) and
**written in zero production code paths** — the only writers in the entire
repository are three test fixtures (`tests/test_artifact_memory.py:130`, `:192`,
`:223`). With no approvals file, `_approved_groups()` returns an empty set at
`:286` and every query is filtered to nothing.

So the shipped surface advertises a tool that is guaranteed to return an empty
list on every install. That is the defect to fix regardless of which way the
keep/retire decision goes.

**What does exist, and is good:** `workspace-tooling/graphiti_policy.py` is a
real ontology — typed entities, a `Supersedes` edge (`:33`, `:143`), and
per-pair allowed-edge maps (`:166-190`) with pre-persistence validation
(`validate_extracted_nodes` `:225`, `validate_extracted_edges` `:288`). This is
the fail-closed write contract the audit recommended generalising, and it is
worth keeping *whatever happens to Graphiti itself*.

## 2. The cost of KEEP

### Runtime dependencies it adds

| Dependency | Where declared | macOS | Linux | Windows |
|---|---|---|---|---|
| `graphiti-core==0.29.2` | `requirements-artifact-ingestion.lock.txt:33` | ✅ | ✅ | ✅ (pure Python) |
| `FalkorDB==1.6.1` (client) | `lock.txt:28` | ✅ | ✅ | ✅ (pure Python) |
| `neo4j==6.2.0` | `lock.txt:47` | ✅ | ✅ | ✅ — pulled in even though only the `[falkordb]` extra is used |
| **FalkorDB server** (container) | **nowhere** — absent from `services/qdrant/compose.yaml` | via Docker | native/Docker | **Docker Desktop only** |
| LLM endpoint | `graphiti_pilot.py:35` → `127.0.0.1:11434/v1` | ✅ | ✅ | ✅ |
| Embedding model `nomic-embed-text`, 768-dim | `graphiti_pilot.py:37-38` | ✅ | ✅ | ✅ |

### Measured on this machine (Windows 11, Docker 29.1.3)

| Measurement | Value |
|---|---|
| `falkordb/falkordb:latest` published platforms | **`linux/amd64` and `linux/arm64` only** — no `windows/*` image exists |
| Image size on disk | **582 MB** |
| Pull wall-clock | 7 s (warm network) |
| Container start → `PING`/`PONG` | 5 s |
| Idle RAM, no data | **116 MiB** |

That 582 MB is a *second* container service: `compose.yaml` today declares only
`qdrant` (`:4`) and `qdrant-restore` (`:50`). Keeping Graphiti means shipping
and supervising a second stateful backend.

### What I could NOT measure, and why

**The end-to-end pilot ingest was not run.** I want to be precise about this
rather than estimate it:

- `graphiti_pilot.py plan` fails immediately with
  `ERROR: [Errno 2] No such file or directory: /home/cdare/.local/share/agent-kit`
  — it needs a provisioned derived root and an outbox of chunks
  (`DEFAULT_OUTBOX`, `graphiti_pilot.py:33`).
- None of `graphiti_core`, `falkordb`, `pydantic` or `openai` are importable
  outside the provisioned venv.
- This machine's Ollama has three models (`qwen2.5-coder:7b`, `qwen3:8b`,
  `qwen3:14b`) and **no embedding model at all** — the configured
  `nomic-embed-text` is absent and would have to be pulled.

The honest reading: a measured ingest requires provisioning the entire substrate
first. **That is itself a finding.** The "keep" branch's true cost is not the
582 MB container; it is that the temporal-graph leg cannot be exercised — by a
contributor, by CI, or by this spike — without standing up Qdrant *and*
FalkorDB *and* a venv *and* an embedding model. Anything that hard to run is
hard to keep correct, which is consistent with how it reached a state where its
approvals file had no writer and nobody noticed.

### The deeper tension

Two of the three costs point straight at stated goals:

- **An LLM in the ingest path** makes ingestion non-deterministic, in a pipeline
  whose defining property is that it is hash-verified and fail-closed. Re-running
  ingest with a different model, or the same model at a different temperature,
  produces different graph content for identical input.
- **A container-only graph DB** contradicts "runs on any OS" for the substrate
  more sharply than the rest of the substrate does. The Python modules can be
  ported (that is M2/M5); a linux-only image cannot be ported at all, only
  virtualised.

## 3. The cost of RETIRE

**Capability lost:** multi-hop and time-aware queries over extracted facts —
"what superseded decision X", "what did we believe about Y in March". Vector and
lexical retrieval answer *what a document says*; they do not answer *how facts
relate or when they stopped being true*. The `Supersedes` edge type
(`graphiti_policy.py:33`) is the shape of that capability.

**Work that closes as won't-do if the pilot is retired:**

- **F044** — tokenized, time-filtered graph retrieval to replace the whole-query
  `CONTAINS` lookup in `temporal_facts`. Scheduled in M9 (`memory-grows-and-tiers`).
- **F068** — applying `invalid_at` when reading facts, so invalidated facts stop
  being returned as live. Also M9.

Both are improvements *to* the graph leg. With no graph leg, neither has a
subject. Nothing else in M8/M9 depends on them.

**Not lost:** `graphiti_policy.py`. Its validation contract is
substrate-independent and the audit already recommends lifting it into a general
`memory_write_contract` that gates every write regardless of destination. Retiring
Graphiti does not retire that idea, and this memo recommends keeping the module
in-tree either way.

## 4. Recommendation

> **Defer: unregister `query_temporal_facts` from the shipped tool surface, keep
> the pilot and `graphiti_policy.py` in-tree behind an opt-in extra, and re-decide
> after M8's retrieval gold set exists.**

One recommendation, as asked. The reasoning is that keep-vs-retire is currently
being argued on intuition, and M8 is already building the instrument that would
settle it — a reproducible gold set with recall/nDCG measurement. Deciding *after*
that exists costs one milestone of delay and converts an opinion into a
measurement. Meanwhile the actual live defect — a tool that always returns
empty — is fixed immediately by unregistering it, which is a change worth making
under either outcome.

This deliberately does **not** delete anything. Goal D names Graphiti explicitly;
this keeps the capability reachable for anyone who provisions it, while removing
it from the surface the kit *promises*.

**Confidence: moderate.** High that unregistering the tool is right (a tool that
cannot return data should not be advertised — that part is not really a judgement
call). Moderate on the defer-rather-than-commit half, because it trades a
milestone of delay for evidence.

**The single fact that would change it:** evidence that the temporal/multi-hop
query is *actually asked* — either a real retrieval failure on the gold set that
graph traversal closes and reranking does not, or an adopter asking for it. If
that shows up before M8, promote the pilot rather than deferring it. Conversely,
if the gold set shows hybrid retrieval already answers the questions people ask,
retire it outright and close F044/F068 as won't-do.

## 5. Open questions this spike did not settle

1. **FalkorDB's licence.** Not present in the image labels, and I did not verify
   it from a primary source. It matters: this repository is MIT, and a
   source-available server licence (as several Redis-lineage projects use) would
   constrain how the kit may bundle or recommend it. **Confirm before any
   `compose.yaml` change lands.**
2. **Neo4j as an alternative backend** — `neo4j==6.2.0` is already in the lock.
   Not evaluated; it would change the container-size and licence answers.
3. **Whether the LLM extraction step can be made deterministic enough** (fixed
   seed, pinned model digest, cached extractions) to sit inside a hash-verified
   pipeline. If it can, the strongest objection to "keep" weakens considerably.

---

_Evidence in this memo was gathered by reading the cited files and by running the
measurements shown. The container and image pulled for the measurements were
removed afterwards._
