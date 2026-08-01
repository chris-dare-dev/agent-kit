#!/usr/bin/env python3
"""Create, independently restore, and verify a loopback Qdrant snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

import artifact_qdrant_migrate as migration
import artifact_runtime
import artifact_security as security


class SnapshotError(RuntimeError):
    """Snapshot creation, restore, or verification failed."""


def _compose(
    runtime: artifact_runtime.ArtifactRuntime,
    *arguments: str,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    root = runtime.qdrant_admin_key_file.parent
    result = subprocess.run(
        [
            shutil.which("docker") or "/usr/local/bin/docker",
            "compose",
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "compose.yaml"),
            "--profile",
            "restore-test",
            *arguments,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise SnapshotError(
            "docker compose failed: " + (result.stderr or result.stdout)[-2000:]
        )
    return result


def _download(
    runtime: artifact_runtime.ArtifactRuntime,
    snapshot_name: str,
    destination: Path,
) -> tuple[str, int]:
    destination = destination.expanduser().absolute()
    directory = security.ensure_private_directory(destination.parent)
    temporary = directory / f".tmp-{destination.name}-{os.getpid()}-{uuid.uuid4().hex}"
    request = urllib.request.Request(
        runtime.qdrant_url
        + "/collections/"
        + quote(runtime.qdrant_collection, safe="")
        + "/snapshots/"
        + quote(snapshot_name, safe=""),
        method="GET",
        headers={"api-key": runtime.qdrant_admin_key()},
    )
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(
        temporary,
        os.O_WRONLY | getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise SnapshotError(
                    f"snapshot download returned HTTP {response.status}"
                )
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)
                    digest.update(block)
                    size += len(block)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, destination)
        security.secure_created_file(destination)
        security.fsync_directory(directory)
        return digest.hexdigest(), size
    finally:
        temporary.unlink(missing_ok=True)


def _wait_restore(runtime: artifact_runtime.ArtifactRuntime) -> None:
    import urllib.error

    key = artifact_runtime.read_secret(
        runtime.qdrant_admin_key_file.parent / "restore-read-only-api-key",
        "restore read-only key",
    )
    for _attempt in range(60):
        request = urllib.request.Request(
            "http://127.0.0.1:6345/collections",
            headers={"api-key": key},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise SnapshotError("independent restore Qdrant did not become healthy")


def _selection_ids(path: Path) -> dict[str, dict[str, Any]]:
    security.require_private_file(path)
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as connection, connection:
        return {
            str(point_id): json.loads(str(unit_json))
            for point_id, unit_json in connection.execute(
                "SELECT point_id, unit_json FROM selected_units ORDER BY point_id"
            )
        }


def exercise(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    selection: Path,
) -> dict[str, Any]:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise SnapshotError("Qdrant client dependency is unavailable") from exc
    main = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_admin_key(),
        timeout=180,
    )
    snapshot_started = time.monotonic()
    snapshot = main.create_snapshot(runtime.qdrant_collection, wait=True)
    if snapshot is None:
        raise SnapshotError("Qdrant did not return a snapshot descriptor")
    snapshot_root = security.ensure_private_directory(
        runtime.qdrant_admin_key_file.parent / "snapshots"
    )
    downloaded = snapshot_root / str(snapshot.name)
    download_digest, download_size = _download(
        runtime,
        str(snapshot.name),
        downloaded,
    )
    if snapshot.checksum and download_digest != str(snapshot.checksum):
        raise SnapshotError(
            f"download digest {download_digest} != server checksum {snapshot.checksum}"
        )
    if int(snapshot.size or 0) != download_size:
        raise SnapshotError(
            f"download size {download_size} != server size {snapshot.size}"
        )
    snapshot_seconds = time.monotonic() - snapshot_started

    restore_root = runtime.qdrant_admin_key_file.parent / "restore-storage"
    security.require_private_directory(restore_root)
    collections_root = restore_root / "collections"
    if collections_root.exists() and any(collections_root.iterdir()):
        raise SnapshotError(
            "independent restore storage already has collections; refusing to overwrite it"
        )
    _compose(runtime, "up", "-d", "qdrant-restore")
    _wait_restore(runtime)
    restore_key = artifact_runtime.read_secret(
        runtime.qdrant_admin_key_file.parent / "restore-admin-api-key",
        "restore admin key",
    )
    restore = QdrantClient(
        url="http://127.0.0.1:6345",
        api_key=restore_key,
        timeout=300,
    )
    corrupt_probe = snapshot_root / "corrupt-restore-probe.snapshot"
    with downloaded.open("rb") as source:
        truncated = source.read(1024 * 1024)
    security.atomic_write_bytes(corrupt_probe, truncated, replace=corrupt_probe.exists())
    corrupt_rejected = False
    try:
        with corrupt_probe.open("rb") as handle:
            restore.http.snapshots_api.recover_from_uploaded_snapshot(
                collection_name=runtime.qdrant_collection + "_corrupt_probe",
                wait=True,
                priority=models.SnapshotPriority.SNAPSHOT,
                checksum=hashlib.sha256(truncated).hexdigest(),
                snapshot=handle,
            )
    except Exception:
        corrupt_rejected = True
    if not corrupt_rejected:
        raise SnapshotError("truncated/corrupt snapshot was unexpectedly accepted")

    restore_started = time.monotonic()
    with downloaded.open("rb") as handle:
        restored = restore.http.snapshots_api.recover_from_uploaded_snapshot(
            collection_name=runtime.qdrant_collection,
            wait=True,
            priority=models.SnapshotPriority.SNAPSHOT,
            checksum=download_digest,
            snapshot=handle,
        )
    if not bool(restored.result):
        raise SnapshotError("independent snapshot restore did not report success")
    restore_seconds = time.monotonic() - restore_started

    expected = _selection_ids(selection)
    restored_points = migration._scroll_payloads(
        restore,
        runtime.qdrant_collection,
    )
    exact_ids = set(restored_points) == set(expected)
    sample_ids = migration._sample(sorted(expected), 100)
    hash_failures: list[str] = []
    for point_id in sample_ids:
        unit = expected[point_id]
        payload = restored_points.get(point_id, {})
        for field in (
            "unit_id",
            "revision_id",
            "content_sha256",
            "chunk_sha256",
        ):
            if str(payload.get(field)) != str(unit[field]):
                hash_failures.append(f"{point_id}:{field}")
    passed = (
        exact_ids
        and not hash_failures
        and len(restored_points) == len(expected)
        and corrupt_rejected
    )
    evidence = {
        "schema_version": 1,
        "verified_at": datetime.now().astimezone().isoformat(),
        "passed": passed,
        "snapshot": {
            "name": snapshot.name,
            "sha256": download_digest,
            "bytes": download_size,
            "create_and_download_seconds": round(snapshot_seconds, 3),
            "path": str(downloaded),
        },
        "restore": {
            "independent_storage": str(restore_root),
            "independent_url": "http://127.0.0.1:6345",
            "elapsed_seconds": round(restore_seconds, 3),
            "rto_target_seconds": 7200,
            "points": len(restored_points),
            "exact_point_id_set": exact_ids,
            "hash_sample_size": len(sample_ids),
            "hash_failures": hash_failures,
            "corrupt_snapshot_rejected": corrupt_rejected,
        },
        "canonical_mutation": "disabled",
        "embedded_mutation": "disabled",
    }
    evidence_path = snapshot_root / "snapshot-restore-evidence.json"
    security.atomic_write_json(evidence_path, evidence, replace=True)
    restore.close()
    main.close()
    _compose(runtime, "stop", "qdrant-restore")
    if not passed:
        raise SnapshotError(f"snapshot/restore verification failed: {evidence_path}")
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=artifact_runtime.DEFAULT_CONFIG,
    )
    parser.add_argument(
        "--selection",
        type=Path,
        default=migration.DEFAULT_SELECTION,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = exercise(
            runtime=artifact_runtime.load_runtime(args.config),
            selection=args.selection,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
