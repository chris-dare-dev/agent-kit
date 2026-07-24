#!/usr/bin/env python3
"""Keep Docker Desktop and the private loopback Qdrant container available.

This job also carries the pipeline watchdog.  It already runs every 300 s and
it is not the consumer, so it can assert what the consumer structurally
cannot: that the consumer is loaded and has run recently.  The watchdog phase
runs on every invocation, including one where the Docker phase failed, and it
never changes this job's own exit status — a Qdrant outage must not silence
consumer alerting, and a watchdog fault must not mask a Qdrant outage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import artifact_runtime
import artifact_security as security
import artifact_watchdog


# Resolve the CLI from PATH first: /usr/local/bin/docker is the Docker Desktop
# location on macOS only (Linux/WSL integration provisions /usr/bin/docker).
DOCKER = Path(shutil.which("docker") or "/usr/local/bin/docker")
SERVICE_ROOT = artifact_runtime.DEFAULT_DERIVED_ROOT / "services" / "qdrant"
HEALTH = artifact_runtime.DEFAULT_DERIVED_ROOT / "artifact-qdrant-bootstrap-health.json"


def _run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DOCKER), *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_watchdog() -> dict[str, object] | None:
    """Run the consumer watchdog; never let its outcome change ours."""
    try:
        return artifact_watchdog.run()
    except Exception as exc:  # noqa: BLE001 - watchdog faults are reported, not fatal
        print(f"watchdog phase failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def main() -> int:
    status = "healthy"
    detail = ""
    try:
        if not DOCKER.is_file():
            raise RuntimeError(f"Docker CLI is missing: {DOCKER}")
        if sys.platform == "darwin":
            desktop = _run(["desktop", "status"], 30)
            if desktop.returncode != 0:
                started = _run(["desktop", "start"], 120)
                if started.returncode != 0:
                    raise RuntimeError(
                        "Docker Desktop did not start: "
                        + (started.stderr or started.stdout)[-1000:]
                    )
        else:
            # The `docker desktop` CLI plugin is macOS-only. Inside WSL the
            # engine belongs to the Windows side; probe it and report — the
            # 300 s cadence retries once Docker Desktop is up.
            #
            # The exit code alone is NOT a usable signal here: when Docker
            # Desktop is stopped, the WSL-integration `docker` shim exits 0
            # with EMPTY stdout, so a returncode-only check reported the
            # bootstrap healthy while Qdrant was dead (observed 2026-07-22
            # after a reboot). Require the version string itself.
            probe = _run(["info", "--format", "{{.ServerVersion}}"], 30)
            version = probe.stdout.strip()
            if probe.returncode != 0 or not version:
                raise RuntimeError(
                    "Docker engine unreachable from this distro — start Docker "
                    "Desktop on the Windows host (WSL integration provides the "
                    "CLI socket): "
                    + ((probe.stderr or probe.stdout).strip() or
                       f"`docker info` exited {probe.returncode} with no server version")[-1000:]
                )
        compose = _run(
            [
                "compose",
                "--env-file",
                str(SERVICE_ROOT / ".env"),
                "-f",
                str(SERVICE_ROOT / "compose.yaml"),
                "up",
                "-d",
                "qdrant",
            ],
            120,
        )
        if compose.returncode != 0:
            raise RuntimeError(
                "Qdrant compose bootstrap failed: "
                + (compose.stderr or compose.stdout)[-1000:]
            )
        # Post-condition: "healthy" must mean the container is actually
        # RUNNING, not merely that the commands above exited 0. Without this
        # the health file can assert a serving Qdrant that does not exist.
        running = _run(
            [
                "compose",
                "--env-file",
                str(SERVICE_ROOT / ".env"),
                "-f",
                str(SERVICE_ROOT / "compose.yaml"),
                "ps",
                "--quiet",
                "--status",
                "running",
                "qdrant",
            ],
            30,
        )
        if running.returncode != 0 or not running.stdout.strip():
            raise RuntimeError(
                "Qdrant container is not running after compose up: "
                + ((running.stderr or running.stdout).strip() or "no running container id")[-1000:]
            )
    except Exception as exc:
        status = "failed"
        detail = f"{type(exc).__name__}: {exc}"[:2000]
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "detail": detail,
    }
    _run_watchdog()
    try:
        security.atomic_write_json(HEALTH, payload, replace=True)
    except Exception as exc:
        print(f"bootstrap health write failed: {exc}", file=sys.stderr)
        return 2
    if status != "healthy":
        print(detail, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
