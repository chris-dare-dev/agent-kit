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
`workspace-tooling/requirements-artifact-ingestion.lock.txt` **with
`--require-hashes`**, and provisions the Qdrant container.

The lockfile is hash-pinned by design: a mirror that rewrites artifacts fails
here rather than silently installing something else.

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
