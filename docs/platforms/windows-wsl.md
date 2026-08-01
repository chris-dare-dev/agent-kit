---
project: agent-kit
type: doc
authorship: agent-generated
tags:
- project/agent-kit
- type/doc
- authorship/agent-generated
---

# Running agent-kit on Windows, with WSL2 for the resident service

**This is an interim path.** A native Windows transport for the resident
artifact-memory service is milestone
[M5 (native-everywhere)](https://github.com/chris-dare-dev/agent-kit/milestone/5).
Until it lands, WSL2 is how a Windows host gets a working memory service. Nothing
here is a permanent architectural commitment, and none of it is a special build:
the WSL2 guest runs ordinary Linux code.

## What actually needs WSL2 (and what does not)

Most of this repository runs natively on Windows. Reach for WSL2 for one thing.

| | Windows native | Needs WSL2 |
|---|---|---|
| MCP server (13 of 17 tools) | ✅ | |
| Generators, the seven gates, `npm test` | ✅ | |
| Python substrate — imports and collects | ✅ | |
| Resident memory service + the 4 artifact-memory tools | | ✅ |
| Shell hooks (`data/hooks/*.sh`, bash + jq) | | ✅ |

The reason is narrow: `artifact_memory_service.py` listens on a **Unix-domain
socket**, and Windows has no AF_UNIX. Run it on Windows and it exits 2 in under a
second naming the platform and pointing here — it does not hang or emit a wall of
import errors.

Everything else was made portable in
[M2](https://github.com/chris-dare-dev/agent-kit/milestone/2): `fcntl`,
`os.geteuid`, `resource` and the `UnixStreamServer` base now go through
[`workspace-tooling/platform_compat.py`](../../workspace-tooling/platform_compat.py).
Substrate collection on Windows went from 260 tests to 625.

## Where the repository must live

**Clone into the WSL filesystem (`~/agent-kit`), not `/mnt/c`.** This is not a
preference and the setup script refuses the alternative.

`/mnt/c` is a 9p network mount into the Windows filesystem. Two consequences:

- **File locking is not the same primitive.** The substrate takes advisory locks
  (`platform_compat.exclusive_file_lock`) around outbox publication, quarantine
  and the service's single-instance guard. Their behaviour across the 9p
  boundary is not the behaviour they are written against, and a lock that
  silently fails to exclude is worse than no lock — it corrupts quietly.
- **Small-file I/O is roughly an order of magnitude slower.** The substrate does
  a great deal of it (a SQLite catalog, a gzip-JSONL outbox, per-chunk embedding).

So you will have **two clones**: one on Windows for the MCP server and the
generators, one inside WSL for the substrate. They are independent checkouts of
the same repository; nothing is shared between them at runtime.

Derived state lives inside the guest at `~/.local/share/agent-kit/` (or
`$XDG_DATA_HOME/agent-kit`, or `$AGENT_KIT_DERIVED_ROOT`). It is never written to
`/mnt/c`.

## Setup

### 1. Install WSL2 and a distribution

In an **elevated PowerShell** on the Windows host:

```powershell
wsl --install
```

Reboot when it asks. That installs WSL2 and Ubuntu. Verify — `VERSION` must be
`2`, not `1`:

```powershell
wsl -l -v
```

If you already had WSL1: `wsl --set-version <distro> 2`.

### 2. Install the guest prerequisites

Inside the guest (`wsl` from PowerShell, or the distro's terminal):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Docker must be reachable from inside the guest. Either enable **WSL integration**
for this distro in Docker Desktop's settings, or install Docker in the guest
directly. Check:

```bash
docker info >/dev/null && echo ok
```

### 3. Clone into the Linux filesystem

```bash
git clone https://github.com/chris-dare-dev/agent-kit ~/agent-kit
cd ~/agent-kit
```

### 4. Provision

```bash
scripts/wsl/setup.sh            # read-only plan: prints what it would do
scripts/wsl/setup.sh --apply    # create the venv, install pinned deps, start Qdrant
```

The plan run writes nothing. `--apply` creates
`~/.local/share/agent-kit/venv`, installs
`workspace-tooling/requirements-artifact-ingestion.lock.txt`, and provisions the
Qdrant container.

That lockfile is **version-pinned, not hash-pinned**, which is its documented
design rather than an oversight: `onnxruntime`, `numpy`, `tokenizers` and
`pillow` ship per-platform wheels, so a hash-pinned lock captured on one platform
does not install on another. Per-platform hash locks are a tracked follow-up
(F-10). So the lock protects you from silent dependency *drift* — the transitive
closure cannot change without a diffable commit — but not from a mirror that
rewrites artifacts. Do not assume the stronger property.

It was captured on **darwin/arm64 under Python 3.12.13**. On a different
platform or interpreter some pins may not resolve; the pip output will say which.

### 4b. Bootstrap the corpus — INCOMPLETE, read this

> **`setup.sh --apply` does not get you to a running service, and this document
> will not pretend otherwise.** Provisioning creates the runtime *config*; it
> does not create the derived state that config points at, and the resident
> service refuses to start until every referenced path exists (fail-closed, by
> design — see the design rules in the root README).
>
> The repository documents no fresh-install bootstrap. The chain below was
> established empirically by running it, and the last step has no known command.

Steps that work, in order (`VP=~/.local/share/agent-kit/venv/bin/python`,
`D=~/.local/share/agent-kit`, `PYTHONPATH=$PWD/workspace-tooling`):

1. **Catalog** — needs a policy; the shipped example is a starting point, not a
   recommendation for your tree.
   `$VP workspace-tooling/artifact_catalog.py --workspace "$PWD" --policy workspace-tooling/artifact-policy.example.json`
2. **Outbox** —
   `$VP workspace-tooling/artifact_ingestion.py prepare --catalog $D/artifact-catalog.sqlite3 --output-root $D/outbox`
3. **Embed + index** (needs `QDRANT_API_KEY` from
   `$D/services/qdrant/admin-api-key`) —
   `$VP workspace-tooling/artifact_ingestion.py qdrant --catalog $D/artifact-catalog.sqlite3 --outbox <run-dir> --state $D/ingestion-state.sqlite3 --qdrant-url <url> --collection <name> --apply`
4. **Consumer state** — circular on a fresh install: the consumer needs the
   runtime config, which validates a file only the consumer creates. Break it
   once with the offline flag —
   `$VP workspace-tooling/artifact_event_consumer.py consume --state $D/artifact-event-consumer.sqlite3 --no-runtime-config --qdrant-path $D/qdrant --apply`
5. **Private directories** the config names but nothing creates
   (`$D/outbox`, `$D/skill-events`, `$D/qdrant`) — use
   `artifact_security.ensure_private_directory`, not `mkdir`, so the modes are right.

**Where it stops.** The service then requires
`$D/services/qdrant/build-manifest.json`. The only writer of that file is
`artifact_qdrant_migrate.py backfill` — a *migration* tool, which refuses with
`no completed checkpoints for target local:…` because it resolves the embedded
backend and generation `v1`, not the server backend and generation the runtime
config actually uses. There is no fresh-install command that produces it.

So on a clean machine the resident service cannot currently be started. That is
a substrate bootstrap gap, not a WSL2 gap — it would block a fresh Linux or
macOS install identically. Everything up to it works; `scripts/wsl/setup.sh`
reports which of these artifacts are present and which are missing.

### 5. Verify end to end

Inside the guest:

```bash
scripts/wsl/smoke.sh
```

Or from the Windows host, against the guest clone:

```bash
node scripts/wsl-smoke.mjs --guest-path '~/agent-kit'
```

The smoke script prints the socket path and the derived-state root before doing
anything, starts the service, issues one `/v1/search` query over the Unix socket,
asserts the response is a structured JSON object carrying `results`, shuts the
service down, and exits 0.

It asserts the response **shape**, not a hit count: an empty index legitimately
returns zero hits, and asserting otherwise would make a correctly-working fresh
install fail.

Exit codes: `0` round-tripped · `1` a step failed · `2` WSL2 unavailable (host
driver only) · `3` the substrate is not provisioned — a precondition, not a
defect.

## Pointing the MCP server at the guest service

The Windows-side MCP server probes for the socket at startup
(`probeArtifactMemory`) and, not finding one, serves 13 tools instead of 17 and
says so on stderr. That is the correct behaviour today: the socket lives inside
the guest, and a Windows process cannot connect to an AF_UNIX socket regardless
of where it is.

So for now, either:

- run the **MCP server inside WSL too**, so both sides sit in the same
  filesystem and socket namespace; or
- accept 13 tools on the Windows host and use the guest for substrate work.

Bridging a guest Unix socket to a Windows client is exactly the transport work
M5 covers. Do not try to relay it with a hand-rolled TCP proxy: the service's
security model assumes an owner-private socket path with mode `0600` and
verifies it (`socket-insecure` is one of the four probe outcomes), and a TCP
relay discards that guarantee.

## Troubleshooting

**`smoke.sh` exits 3.** The substrate is not provisioned in this guest. Run
`scripts/wsl/setup.sh --apply`.

**`wsl-smoke.mjs` exits 2 saying WSL2 is unavailable.** Either `wsl.exe` is not
on PATH, or WSL is installed with no distribution (`wsl --install -d Ubuntu`).
The probe is bounded at two seconds so this can never be the thing that hangs.

**`setup.sh` refuses because the clone is on `/mnt/c`.** Intended — see *Where
the repository must live*. Clone into `~` instead.

**Docker "installed but not running".** Start Docker Desktop and confirm WSL
integration is enabled for this distro, or `sudo service docker start` if you
installed Docker inside the guest.

**The suite reports failures on Windows.** Expected, and tracked:
`workspace-tooling/run-substrate-tests.py` carries a recorded per-platform
baseline and tells you whether a run is at, above or below it. Windows residue is
[#70](https://github.com/chris-dare-dev/agent-kit/issues/70).
