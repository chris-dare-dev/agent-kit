#!/usr/bin/env python3
"""Take a scheduled, verified Qdrant snapshot and alert on retention pressure.

This is the CADENCE job. It is deliberately NOT `artifact_qdrant_snapshot.py`,
which is the restore *drill*: that script spins up a second Qdrant on :6335 and
refuses to run when `restore-storage/collections` is populated, so it hard-fails
on every run after the first and cannot be scheduled. This job only creates,
downloads, and verifies a snapshot against the server-reported checksum.

Retention is accumulate-and-alert, never auto-delete: ADR-002 §7 and the runtime
config's `deletion_requires_separate_approval` make pruning a gated human action.
When the budget is exceeded this job still takes the snapshot and raises an alert
-- a skipped snapshot is a silent RPO breach, a full disk is a loud one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import artifact_qdrant_snapshot as drill
import artifact_runtime
import artifact_security as security

HEALTH_FILENAME = "artifact-backup-snapshot-health.json"
SCHEMA_VERSION = 1

# Retention budget. Exceeding it alerts; it never deletes. See policy §4.
WARN_COUNT = 7
ALARM_COUNT = 14
WARN_BYTES = 6 * 1024**3
ALARM_BYTES = 12 * 1024**3


class SnapshotCadenceError(RuntimeError):
    """The scheduled snapshot could not be completed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_root(runtime: artifact_runtime.ArtifactRuntime) -> Path:
    return security.ensure_private_directory(
        runtime.qdrant_admin_key_file.parent / "snapshots"
    )


def _existing_snapshots(root: Path) -> list[Path]:
    """Real collection snapshots, newest first.

    Excludes the drill's `corrupt-restore-probe.snapshot`, which is a
    deliberately truncated artifact and not a recovery point.
    """
    snapshots = [
        path
        for path in root.glob("*.snapshot")
        if path.is_file() and path.name != "corrupt-restore-probe.snapshot"
    ]
    return sorted(snapshots, key=lambda p: p.stat().st_mtime, reverse=True)


def _protected_snapshots(root: Path) -> set[str]:
    """Snapshots that rotation must never remove.

    The 2026-07-18 restore drill's evidence file names the snapshot it verified.
    That snapshot is the proof the restore path works, not a routine recovery
    point, so it is exempt from rotation regardless of age.
    """
    protected: set[str] = {"corrupt-restore-probe.snapshot"}
    evidence = root / "snapshot-restore-evidence.json"
    if evidence.exists():
        try:
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            name = (payload.get("snapshot") or {}).get("name")
            if name:
                protected.add(str(name))
        except (OSError, ValueError):
            # Unreadable evidence means protect nothing extra -- but also do not
            # let a parse error silently widen what rotation may delete.
            return {"corrupt-restore-probe.snapshot", "__unreadable-evidence__"}
    return protected


def prune(root: Path, keep: int) -> dict[str, Any]:
    """Rotate old snapshots, keeping the newest `keep`.

    SCOPE: this rotates *backup copies this system created*. It is deliberately
    NOT the ADR-002 §7 generation-deletion gate, which governs deleting a vector
    store generation or the embedded rollback store -- neither of which this
    touches. Drill evidence is protected (see `_protected_snapshots`).
    """
    if keep < 1:
        raise SnapshotCadenceError("keep must be at least 1")
    protected = _protected_snapshots(root)
    if "__unreadable-evidence__" in protected:
        return {
            "pruned": [],
            "skipped": "restore-evidence unreadable; refusing to rotate",
        }
    candidates = [p for p in _existing_snapshots(root) if p.name not in protected]
    removed: list[dict[str, Any]] = []
    for path in candidates[keep:]:
        size = path.stat().st_size
        path.unlink()
        removed.append({"name": path.name, "bytes": size})
    return {
        "pruned": removed,
        "kept": keep,
        "protected": sorted(protected),
        "reclaimed_bytes": sum(item["bytes"] for item in removed),
    }


def newest_age_days(root: Path) -> float | None:
    snapshots = _existing_snapshots(root)
    if not snapshots:
        return None
    mtime = datetime.fromtimestamp(snapshots[0].stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 86400


def assess_retention(root: Path) -> dict[str, Any]:
    """Report retention pressure. Never deletes anything."""
    snapshots = _existing_snapshots(root)
    total = sum(path.stat().st_size for path in snapshots)
    count = len(snapshots)

    if count > ALARM_COUNT or total > ALARM_BYTES:
        level = "alarm"
    elif count > WARN_COUNT or total > WARN_BYTES:
        level = "warn"
    else:
        level = "ok"

    assessment: dict[str, Any] = {
        "level": level,
        "count": count,
        "bytes": total,
        "gigabytes": round(total / 1024**3, 2),
        "warn_at": {"count": WARN_COUNT, "gigabytes": WARN_BYTES / 1024**3},
        "alarm_at": {"count": ALARM_COUNT, "gigabytes": ALARM_BYTES / 1024**3},
        "pruning": "gated-human-action",
        "auto_delete": "disabled",
    }

    if level != "ok":
        # Name the candidates so a human can act, but never act on them here.
        stale = snapshots[WARN_COUNT:]
        assessment["prune_candidates"] = [path.name for path in stale]
        assessment["prune_candidate_bytes"] = sum(p.stat().st_size for p in stale)
        assessment["prune_requires"] = (
            "separate approval per ADR-002 §7 / runtime "
            "rollback.deletion_requires_separate_approval"
        )
    return assessment


def capture(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    root: Path,
) -> dict[str, Any]:
    """Create, download, and checksum-verify one snapshot."""
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise SnapshotCadenceError("Qdrant client dependency is unavailable") from exc

    client = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_admin_key(),
        timeout=300,
    )
    try:
        snapshot = client.create_snapshot(runtime.qdrant_collection, wait=True)
        if snapshot is None:
            raise SnapshotCadenceError("Qdrant did not return a snapshot descriptor")

        destination = root / str(snapshot.name)
        digest, size = drill._download(runtime, str(snapshot.name), destination)

        # The drill's guarantees, minus the restore: a snapshot we cannot verify
        # is not a recovery point, so a mismatch fails the run loudly.
        if snapshot.checksum and digest != str(snapshot.checksum):
            destination.unlink(missing_ok=True)
            raise SnapshotCadenceError(
                f"download digest {digest} != server checksum {snapshot.checksum}"
            )
        if int(snapshot.size or 0) != size:
            destination.unlink(missing_ok=True)
            raise SnapshotCadenceError(
                f"download size {size} != server size {snapshot.size}"
            )

        return {
            "name": str(snapshot.name),
            "sha256": digest,
            "bytes": size,
            "path": str(destination),
            "collection": runtime.qdrant_collection,
            "generation": runtime.qdrant_generation,
            "checksum_verified": True,
            "restore_verified": False,
        }
    finally:
        client.close()


def _write_health(derived_root: Path, payload: dict[str, Any]) -> Path:
    # Derived from the loaded runtime's own root (config_path.parent), NOT the
    # module default, so a non-default --config apply cannot stamp the production
    # snapshot-health file (the Sol #6 hazard; twin of the off-device fix, folded
    # into M4). For the default config this is DEFAULT_DERIVED_ROOT, so production
    # behavior is unchanged.
    path = derived_root / HEALTH_FILENAME
    security.atomic_write_json(path, payload, replace=path.exists())
    return path


def run(
    *,
    config: Path,
    check_only: bool = False,
    prune_keep: int | None = None,
) -> dict[str, Any]:
    runtime = artifact_runtime.load_runtime(config)
    root = snapshot_root(runtime)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ran_at": _now(),
        "mode": "check-only" if check_only else "snapshot",
        "healthy": False,
        "snapshot": None,
        "error": None,
    }

    try:
        if not check_only:
            payload["snapshot"] = capture(runtime=runtime, root=root)
        payload["healthy"] = True
    except Exception as exc:
        # A failed scheduled snapshot is exactly the silent-failure class F-08
        # describes, so it must land in a durable file, not only in a log.
        payload["error"] = f"{type(exc).__name__}: {exc}"[:1000]

    # Prune only after a successful capture: rotating away old recovery points
    # when the new one failed would turn a bad night into a worse one.
    if prune_keep is not None and not check_only and payload["healthy"]:
        payload["prune"] = prune(root, prune_keep)

    payload["retention"] = assess_retention(root)
    payload["snapshot_root"] = str(root)

    newest = _existing_snapshots(root)
    if newest:
        stat = newest[0].stat()
        age_seconds = (
            datetime.now(timezone.utc)
            - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        ).total_seconds()
        payload["newest_snapshot"] = {
            "name": newest[0].name,
            "age_seconds": int(age_seconds),
            "age_hours": round(age_seconds / 3600, 2),
            "bytes": stat.st_size,
        }
    else:
        payload["newest_snapshot"] = None

    health_path = _write_health(runtime.config_path.parent, payload)
    payload["health_file"] = str(health_path)

    # Durable, greppable alert lines for the launchd error log.
    if payload["error"]:
        print(f"ALERT snapshot-failed {payload['error']}", file=sys.stderr)
    if payload["retention"]["level"] != "ok":
        print(
            f"ALERT snapshot-retention-{payload['retention']['level']} "
            f"count={payload['retention']['count']} "
            f"gb={payload['retention']['gigabytes']} "
            f"pruning=gated-human-action",
            file=sys.stderr,
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=artifact_runtime.DEFAULT_CONFIG,
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="assess retention and snapshot age without taking a snapshot",
    )
    parser.add_argument(
        "--prune-keep",
        type=int,
        help=(
            "after a successful snapshot, rotate away all but the newest N "
            "(drill evidence is always protected; this is backup rotation, "
            "not ADR-002 generation deletion)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(
            config=args.config,
            check_only=args.check_only,
            prune_keep=args.prune_keep,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
