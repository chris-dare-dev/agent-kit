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

> **What the default profile writes to disk.** Under the default
> `SERVER_PROFILE=personal`, the token log keeps a **120-character raw preview of
> every tool call's arguments** (`summarizeArgs`), and the response-cache
> snapshot keeps raw `argsKeys`. Both default to
> `$WORKSPACE_ROOT/.claude/mcp-token-log.jsonl` and
> `$WORKSPACE_ROOT/.claude/mcp-cache-snapshot.jsonl`. `SERVER_PROFILE=shared`
> switches the log to `hashArgs` and writes to distinct `*.shared.jsonl` files.
> Both patterns are gitignored, but if you point `TOKEN_LOG_PATH` or
> `CACHE_SNAPSHOT_PATH` somewhere else, that is on you. Set either to `""` to
> disable it.

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

Optionally, plant the bundled skills, agents, commands and hooks into a
workspace's `.claude/`, and check your machine:

```bash
node scripts/cli.mjs init      # symlink on POSIX, copy on Windows
node scripts/cli.mjs doctor    # one PASS/FAIL/SKIP row per prerequisite
```

`init` refuses to touch anything it did not create unless you pass `--force`,
never writes outside the clone without `--install-to`, and records every path it
touched in `.agent-kit/install-receipt.json`. Run `init --dry-run` to see the
plan first.

The substrate is separate and optional — see
[`workspace-tooling/README.md`](./workspace-tooling/README.md) for provisioning
(Docker + Qdrant + a Python 3.12 venv from the pinned lockfile).

## Supported platforms

This table is what is **measured** today, not what is intended. Every entry was
run on Windows 11 and on Linux; macOS entries are marked as inferred where no
host was available to check them.

**native** — runs here, no workaround. **WSL2** — Windows users run it inside a
WSL2 guest. **partial** — runs and reports, with known residue that is tracked.
**unsupported** — declines by name; see the linked milestone.

| Component | macOS | Linux | Windows |
|---|---|---|---|
| MCP server (`dist/index.js`) | native (17 tools) | native (17 tools) | native (13 tools) |
| Artifact-memory tool group (4 tools, Unix socket) | native | native | unsupported — no AF_UNIX ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)); WSL2 meanwhile |
| Generators + the seven gates | native | native | native |
| TypeScript suite (`npm test`) | native | native | native |
| Python substrate — **imports and collects** | native | native | native |
| Python substrate — **passes** | partial | partial | partial ([#70](https://github.com/chris-dare-dev/agent-kit/issues/70)) |
| Resident memory service (`artifact_memory_service.py`) | native | native | unsupported ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)); WSL2 meanwhile |
| Shell hooks (`data/hooks/*.sh`, bash + jq) | native | native | unsupported — fail open ([M3](https://github.com/chris-dare-dev/agent-kit/milestone/3)) |
| Obsidian vault projection (symlinks, `dir_fd`) | native | native | unsupported ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) |
| Service supervision (launchd `.plist` only) | native | unsupported ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) | unsupported ([M5](https://github.com/chris-dare-dev/agent-kit/milestone/5)) |

**Substrate suite, measured.** The suite used to be un-importable off POSIX;
M2 routed `fcntl`, `os.geteuid` and the `resource`/AF_UNIX dependencies through
`workspace-tooling/platform_compat.py`, so it now collects everywhere. It does
not yet *pass* everywhere:

| | collected | failures | errors |
|---|---|---|---|
| Linux (no venv) | 627 | 1 | 30 |
| Windows 11 | 625 | 31 | 337 |

`workspace-tooling/run-substrate-tests.py` records these as baselines and tells
you whether a run is at, above or below them — so "did I break something" is
answerable without a clean tree to diff against. On POSIX, ~30 of the errors are
cases that import `qdrant_client` at call time and clear once the provisioned
venv is used. The Windows residue (SQLite handles held across tempdir teardown,
macOS-only launchd fixtures) is [#70](https://github.com/chris-dare-dev/agent-kit/issues/70).

**macOS is inferred, not measured.** No macOS host was available. Its rows
assume POSIX parity with Linux, which is what the code implies but not something
anyone has run.

**Unsupported means it says so.** `artifact_memory_service.py` on Windows exits
2 in under a second naming the platform, the missing AF_UNIX support, and the
WSL2 alternative — rather than emitting a cascade of import errors that read like
substrate defects.

On Windows the MCP server starts and serves the 13 non-substrate tools, and says
on stderr exactly which group is unavailable and why.

## Testing

```bash
npm run test:all        # everything below that runs without a provisioned substrate

npm run test:unit       # all 10 TypeScript suites (globbed) + init + uninstall
npm run gates           # every generator gate, consistency check and shell harness
npm run verify:quickstart

# substrate suite — runs on every OS and reports against a recorded baseline
# (Linux 627 collected / 1 fail / 30 error; Windows 625 / 31 / 337). Exits
# non-zero whenever anything failed, matching baseline or not.
npm run test:substrate

# The substrate suite needs the provisioned venv to clear ~30 of those errors:
# they are cases that import qdrant_client at call time. It is NOT part of
# test:all for that reason. Windows residue is tracked as #70.

# `npm run gates` above runs all of these and prints one named line per gate;
# reach for them individually only when you want one gate's own diagnostics.
# No PYTHONUTF8 needed: every text I/O in data/scripts names its encoding, and
# catalog provenance is POSIX-separated, so these are byte-identical on Windows,
# macOS and Linux. Both were verified on Windows AND Linux.
python3 data/scripts/catalog-generate.py --check
python3 data/scripts/generate-adapter-packs.py --check
python3 data/scripts/generate-root-contract.py --check
python3 data/scripts/model-policy-apply.py --check
python3 data/scripts/mcp-server-name-check.py --check
python3 data/scripts/denylist-check.py --check
python3 data/scripts/template-settings-check.py --check
```

The last one is why a rename is survivable: every `mcp__<server>__` tool grant in
`data/**/*.md` must name a server that all three shipped registration templates
register. Change the server name and it tells you every file still carrying the
old grant.

`agent-kit doctor` runs all of the above prerequisite checks in one pass, with
the right encoding, and prints a fix command per failure.

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

**Substrate (optional):** Python 3.12 · Docker (Qdrant). The Python modules and
their test suite import and run on macOS, Linux and Windows; the **resident
memory service** needs a Unix-domain socket, so on Windows it runs inside WSL2 —
see [Supported platforms](#supported-platforms) and
[`docs/platforms/windows-wsl.md`](./docs/platforms/windows-wsl.md).
