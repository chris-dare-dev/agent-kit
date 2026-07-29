# `workspace-tooling/` — artifact-memory substrate + vault projection tooling

Python tooling that runs **against the workspace on an engineer's own machine**
(macOS or Linux; Windows via WSL2), not in a cluster. Two families share this directory:

| Family | Modules | Dependencies |
|---|---|---|
| **Artifact memory** — ingestion, retrieval, eval, security, the long-running service | `artifact_*.py`, `graphiti_*.py` | heavy (`qdrant-client`, `fastembed`, `graphiti-core`) |
| **Vault projection** — Obsidian/roadmap/portfolio rendering | `path_contract.py`, `portfolio_projection.py`, `project-linker.py`, `obsidian_*.py`, `roadmap_*.py`, `vault_*.py` | stdlib only |

The two families do **not** import each other. They live together because they
share `tests/`, are driven by the same LaunchAgents, and were versioned in one
import.

## Why this is in git (finding F-10)

Until 2026-07-18 this tree lived unversioned at `$PERSONAL_WORKSPACE_ROOT/scripts/`.
Dependency pinning was two top-level packages with no transitive lock, so a
`qdrant-client` / `fastembed` / `onnxruntime` / `numpy` update could silently
change embedding numerics or locking behavior with **no diffable record**, and a
replacement machine could not faithfully reproduce the stack. Hash-pinned tar
archives gave byte identity but neither provenance nor reinstallability.

Versioning the tree and locking the transitive closure is what closes the
reproducibility half of that finding. The enforcement half is NOT closed: there
is no CI in this repository yet, so nothing runs this suite automatically. That
is milestone M3.

## Layout

```
workspace-tooling/
├── *.py                                        # flat — modules import siblings directly
├── tests/                                      # unittest; no __init__.py (see below)
├── schemas/                                    # JSON Schema for the portfolio index
├── services/qdrant/compose.yaml                # local Qdrant compose (config, not state)
├── requirements-artifact-ingestion.txt         # the 2 top-level pins
├── requirements-artifact-ingestion.lock.txt    # 53-package resolved closure
└── lock_check.py                               # drift tripwire between the two
```

The flat layout is load-bearing. Several modules do
`sys.path.insert(0, SCRIPT_DIR)` and import siblings by bare module name
(`import artifact_security`, `from path_contract import …`). Introducing
subpackages breaks those imports and the LaunchAgent entry points together — do
not "tidy" it into `src/` without reworking both.

## Install

Requires **Python 3.12**. The suite fails on 3.9 (27 failures) — the modules use
3.10+ syntax, so a wrong interpreter looks like a real regression.

Paths below use the DEFAULT derived root. It resolves in this order, and every
path in this document follows it:

1. `AGENT_KIT_DERIVED_ROOT` if set
2. `$XDG_DATA_HOME/agent-kit` if set
3. per-OS default — `~/.local/share/agent-kit` (Linux),
   `~/Library/Application Support/agent-kit` (macOS),
   `%LOCALAPPDATA%gent-kit` (Windows)

`artifact_runtime.derived_root()` is the single implementation; substitute your
own root below if you set either variable.

```bash
python3.12 -m venv ~/.local/share/agent-kit/venv
~/.local/share/agent-kit/venv/bin/python -m pip install --upgrade pip
# Install the LOCK, not the top-level pins — the lock is the proven closure.
~/.local/share/agent-kit/venv/bin/python -m pip install \
    -r workspace-tooling/requirements-artifact-ingestion.lock.txt
```

The venv lives **outside** the repo, at
`~/.local/share/agent-kit/venv`, because the LaunchAgent plists reference
that absolute interpreter path.

### Runtime state is not in git

All mutable state — catalog and retrieval SQLite databases, health files, the
runtime config, eval outputs (~2.7 GB) — lives in
`~/.local/share/agent-kit/`. Nothing under `workspace-tooling/` is written
at runtime, so there is no state to gitignore here. `services/qdrant/compose.yaml`
is the compose *definition*; the Qdrant volume is Docker-managed.

## Run the tests

**POSIX only.** This substrate imports `fcntl` and `os.geteuid` at module scope
and binds an AF_UNIX socket, so on Windows it cannot be imported, let alone
tested. Use macOS, Linux, or Windows via WSL2 — see
[Supported platforms](../README.md#supported-platforms).

```bash
cd <repo-root>
PYTHONPATH="$PWD/workspace-tooling" \
  ~/.local/share/agent-kit/venv/bin/python \
  workspace-tooling/run-substrate-tests.py
```

`run-substrate-tests.py` prints a single explanatory banner and does nothing on
an unsupported platform, instead of emitting ~169 import errors that look like
substrate defects. It is not a pass — nothing is verified there.

Use the provisioned venv, not the system interpreter: roughly 30 cases import
`qdrant_client` at call time and error without it.

`tests/` has no `__init__.py`, so plain `unittest discover -s tests` raises
`Start directory is not importable`. Both `PYTHONPATH` and `-t` are required if
you invoke `unittest discover` directly.

The suite is **hermetic** — no Qdrant, FalkorDB or network access needed. Every
`127.0.0.1:6333` in `tests/` is a config fixture or mock return value, and the
single `skipUnless` guards PyYAML. Do not add a service container on the
assumption one is required.

## Regenerating the lock

`requirements-artifact-ingestion.lock.txt` is a `pip freeze` of the proven venv,
not a fresh resolution — it records what demonstrably works, including the
platform-specific wheels the suite passed against.

```bash
python -m pip install -r workspace-tooling/requirements-artifact-ingestion.txt
python -m pip freeze          # body of the lock; keep the provenance header
python workspace-tooling/lock_check.py    # must exit 0
```

`lock_check.py` fails the pipeline (exit 2) if the top-level pins and the lock
disagree — the case where someone bumps `requirements-artifact-ingestion.txt` and
forgets the lock, leaving it silently describing the wrong stack.

**Known limitation:** the lock is version-pinned, not hash-pinned. `onnxruntime`,
`numpy`, `tokenizers` and `pillow` ship per-platform wheels, so a hash-pinned lock
generated on darwin/arm64 will not install on the linux/amd64 CI image; genuine
hash pinning needs one lock per platform. Version pinning closes the F-10
drift-and-reproducibility harm; hash pinning would additionally close supply-chain
tampering, and is tracked as follow-up rather than claimed here.

## LaunchAgents

Six user LaunchAgents drive this tree. Three reference it by absolute path
(`artifact-event-consumer`, `artifact-memory-service`, `artifact-qdrant-bootstrap`)
and three resolve it through `$PERSONAL_WORKSPACE_ROOT/scripts/` (`workspace-vault-sync`,
`roadmap-board-refresh`, `vault-reconcile`).

`$PERSONAL_WORKSPACE_ROOT/scripts` is a **symlink into this directory**, which is why
all six keep working unchanged. Repointing the plists at the repo path is optional
cleanup, not a prerequisite. The
`artifact-memory-service` agent is `KeepAlive`; treat it as a live service.
