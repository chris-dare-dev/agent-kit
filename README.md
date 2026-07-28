# agent-kit

A provider-neutral agent kit and a local artifact-memory substrate for a personal
multi-repo workspace.

Two things live here:

1. **A knowledge kit** — skills, agents, entrypoints and references maintained once,
   compiled into per-provider packs (Claude Code, Codex CLI, OpenCode) by generators,
   and served to a Claude Code session over MCP.
2. **An artifact-memory substrate** — a local pipeline that catalogs the workspace's
   canonical Markdown, chunks it into an immutable outbox, embeds it locally, and
   serves semantic search from a loopback Qdrant behind a resident Unix-socket
   service. Nothing leaves the machine.

## Design rules

- **Canonical Markdown is the source of truth.** The search index is *derived* —
  a discovery hint, never an authority. Retrieve the real file before acting.
- **Derived state is rebuildable; canonical state is never rewritten to fix an index.**
- **Fail closed.** A hash mismatch, a missing build manifest, an incomplete backup:
  each stops the pipeline and says so rather than shipping something unverified.
- **Local embeddings only.** A pinned ONNX model (`all-MiniLM-L6-v2`, 384-dim) loaded
  from a digest-checked local snapshot; runtime downloads are refused.

## ⚠️ The rebuild rule — read before editing `src/`

The registered server command runs the **compiled output** (`node dist/index.js`), not
the TypeScript source. An edit under `src/` is invisible until you `npm run build` **and
restart the Claude Code session** (the MCP server is spawned per session). `npm run dev`
runs the source directly, but that is not what Claude Code launches.

Content under `data/` is loaded **once at server startup** — no hot reload, but no
rebuild needed either. Restart the session after editing it.

## Architecture

```
canonical Markdown
      ├─────────────► artifact_catalog.py ──► catalog (sqlite, exact sha256)
      │                                            │
skills │ emit receipts                             ▼
      │                        artifact_ingestion.py ──► immutable gzip-JSONL outbox
      ▼                                            │
 skill-events/  ──► artifact_event_consumer.py ────┤ (checkpointed, dead-lettered)
                                                   ▼
                                    Qdrant (loopback, API-keyed)
                                                   │
                                artifact_memory_service.py (resident, UDS)
                                                   │
                                       MCP tools ──┘  search_artifacts / get_artifact
```

Support plane: an external watchdog (a dead consumer cannot announce its own death),
a composite health watermark, an age-encrypted off-device backup with a fail-closed
`--verify`, a reversible quarantine, and a retention classifier that defaults to RETAIN.

## MCP tools (17)

| Group | Tools |
|---|---|
| Skills / agents | `list_skills`, `get_skill`, `list_agents`, `get_agent` |
| References | `list_references`, `get_reference` |
| Context guides | `list_context_guides`, `get_context_guide` |
| Memory (local, private) | `list_memory`, `get_memory`, `search_memory` |
| Search | `search_platform_knowledge` |
| Artifact memory | `artifact_memory_status`, `search_artifacts`, `get_artifact`, `query_temporal_facts` |
| Diagnostics | `get_token_stats` |

All are read-only. The server exposes exactly one transport (stdio); a static tripwire
test fails if an HTTP/SSE transport is ever imported.

Under `SERVER_PROFILE=shared` the personal-memory tier is excluded from both the search
index and the memory tools, and the artifact-memory tools are not registered at all.

## Quick start

```bash
git clone https://github.com/chris-dare-dev/agent-kit
cd agent-kit
npm ci && npm run build

claude mcp add agent-kit -- node "$PWD/dist/index.js"
```

No environment variables are required: with none set, the server serves the
content bundled in `data/` and resolves everything else relative to the package
root. `PLATFORM_ROOT` (extra content root), `WORKSPACE_ROOT`, `MEMORY_ROOT`,
`CONTEXT_GUIDES_DIR` and `CLAUDE_MD_GLOBS` are optional overrides — see the
header of [`src/config.ts`](./src/config.ts).

That block is executed by `node scripts/verify-quickstart.mjs`, so if it drifts
from what actually works, the check fails.

The substrate is separate and optional — see
[`workspace-tooling/README.md`](./workspace-tooling/README.md) for provisioning
(Docker + Qdrant + a Python 3.12 venv from the pinned lockfile).

## Supported platforms

The MCP server runs everywhere. The substrate does not yet — it imports `fcntl`
and `os.geteuid` at module scope and binds an AF_UNIX socket, so on Windows it
cannot even be imported. That work is scheduled, and this table is what is true
today rather than what is intended.

| Component | macOS | Linux | Windows |
|---|---|---|---|
| MCP server (`dist/index.js`, 13 tools) | ✅ | ✅ | ✅ |
| Artifact-memory tools (4, over a Unix socket) | ✅ | ✅ | ❌ — no AF_UNIX ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) |
| Python substrate + its test suite | ✅ | ✅ | ❌ WSL2 only ([M2](https://github.com/chris-dare-dev/agent-kit/milestone/2)) |
| Shell hooks (`data/hooks/*.sh`, bash + jq) | ✅ | ✅ | ❌ — fail open, do not run ([M3](https://github.com/chris-dare-dev/agent-kit/milestone/3)) |
| Obsidian vault projection (symlinks, `dir_fd`) | ✅ | ✅ | ❌ ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) |
| Service supervision (launchd `.plist` only) | ✅ | ❌ ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) | ❌ ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) |

On Windows the server starts and serves the 13 non-substrate tools, and says on
stderr exactly which group is unavailable and why.

## Testing

```bash
npm test                                   # MCP contract + config + transport lock
npm run verify:quickstart                  # executes the Quick start block above

# substrate suite (593 tests) — POSIX only; declines with a banner on Windows.
# Needs the provisioned venv: ~30 cases import qdrant_client at call time.
python3 workspace-tooling/run-substrate-tests.py

# the four generator gates — all must be clean before committing data/ changes
python3 data/scripts/catalog-generate.py --check
python3 data/scripts/generate-adapter-packs.py --check
python3 data/scripts/generate-root-contract.py --check
python3 data/scripts/model-policy-apply.py --check
```

`AGENTS.md` and `CONTEXT.md` are **generated** — edit the coverage map, then regenerate.

## Layout

| Path | What |
|---|---|
| `src/` | MCP server (TypeScript → `dist/`) |
| `data/skills`, `data/agents`, `data/commands`, `data/references` | the knowledge base |
| `data/scripts` | generators, validators, pipeline tooling |
| `data/hooks` | PreToolUse guards (incl. the plaintext-credential blocker) |
| `workspace-tooling/` | the artifact-memory substrate + its tests |

## Provenance

This kit began as a personal fork of an internal tool I built at work, then was
genericized: the employer-specific integrations, infrastructure facts, and operational
tooling were removed, and the remaining machinery renamed. What is left is the general
architecture — the generators, the guard, the memory substrate — plus content I use on
my own projects.

`src/security/aidefence-rules.ts` transcribes detection patterns from
[ruflo](https://github.com/ruvnet/ruflo) (MIT) as data; see that file's header for the
attribution and the one deliberate deviation.

## Requirements

**Server:** Node ≥ 20. Nothing else — macOS, Linux and Windows alike.

**Substrate (optional):** Python 3.12 · Docker (Qdrant) · macOS or Linux, or
Windows via WSL2. See [Supported platforms](#supported-platforms); the substrate
does not run natively on Windows today.
