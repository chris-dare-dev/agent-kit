#!/usr/bin/env python3
"""Consume immutable skill-artifact receipts into incremental Qdrant outboxes.

Receipts are validated against the current Phase-1 catalog and the canonical
source bytes before any sink write. Each event gets one deterministic,
append-only outbox. Qdrant ingestion is idempotent; Graphiti candidates are
reported but never written by this consumer.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import artifact_catalog as catalog
import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security
import artifact_skill_capture as capture
import artifact_watermark as watermark
import platform_compat


SCHEMA_VERSION = 2
EVENT_HEX = re.compile(r"^[0-9a-f]{64}$")
DIGEST_HEX = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_DERIVED_ROOT = ingestion.DEFAULT_DERIVED_ROOT
DEFAULT_RECEIPT_ROOT = capture.DEFAULT_RECEIPT_ROOT
DEFAULT_STATE = DEFAULT_DERIVED_ROOT / "artifact-event-consumer.sqlite3"
DEFAULT_OUTBOX_ROOT = DEFAULT_DERIVED_ROOT / "outbox"
DEFAULT_DEAD_LETTER_ROOT = DEFAULT_DERIVED_ROOT / "outbox-dead-letter"
DEFAULT_POLICY = Path(__file__).with_name("artifact-policy.json")
DEFAULT_HEALTH = DEFAULT_DERIVED_ROOT / "artifact-event-consumer-health.json"
DEFAULT_SUPERVISOR_LOG = DEFAULT_DERIVED_ROOT / "supervisor-events.jsonl"
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_ARTIFACTS = 5000
RECEIPT_KEYS = {
    "schema_version",
    "event_id",
    "producer",
    "run_id",
    "captured_at",
    "artifacts",
    "routing_summary",
    "safety",
}
ARTIFACT_KEYS = {
    "relative_path",
    "content_sha256",
    "byte_size",
    "mtime_ns",
    "artifact_type",
    "authority_class",
    "lifecycle_hints",
    "source_scope",
    "repository",
    "project",
    "artifact_id",
    "revision_id",
    "routing",
}
ROUTING_SUMMARY = {
    "qdrant": "eligible-for-incremental-ingestion",
    "graphiti": "candidate-filter-only",
    "graphiti_bulk": "disabled",
}


class ConsumerError(ValueError):
    """A receipt cannot be consumed without violating the safety contract."""


class StaleReceiptError(ConsumerError):
    """A valid receipt no longer identifies the authoritative current revision."""


class PoisonEventError(ConsumerError):
    """An event or its published outbox violates an immutable safety contract."""


@dataclass(frozen=True)
class ReceiptProblem:
    path: Path
    code: str
    detail: str
    stage: str = "discovery"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _outside_workspace(path: Path, workspace: Path, label: str) -> Path:
    resolved = path.expanduser().absolute().resolve(strict=False)
    if _inside(resolved, workspace):
        raise ConsumerError(f"{label} must stay outside the source workspace")
    if path.expanduser().absolute().is_symlink():
        raise ConsumerError(f"{label} must not be a symlink: {path}")
    return resolved


def _read_stable_json(path: Path) -> tuple[dict[str, Any], str]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    content = bytearray()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ConsumerError(f"receipt is not a regular file: {path}")
        if before.st_size > MAX_RECEIPT_BYTES:
            raise ConsumerError(
                f"receipt exceeds {MAX_RECEIPT_BYTES} byte safety limit: {path}"
            )
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            content.extend(block)
        after = os.fstat(handle.fileno())
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise ConsumerError(f"receipt changed while being read: {path}")
    try:
        payload = json.loads(bytes(content))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsumerError(f"receipt is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ConsumerError(f"receipt root must be an object: {path}")
    return payload, digest.hexdigest()


def scan_receipts(
    receipt_root: Path,
) -> tuple[list[Path], list[ReceiptProblem], int]:
    """Discover canonical receipts while isolating every unsafe entry."""
    if not receipt_root.exists():
        return [], [], 0
    security.require_private_directory(receipt_root)
    discovered: list[Path] = []
    problems: list[ReceiptProblem] = []
    temporary_count = 0
    for shard in sorted(receipt_root.iterdir(), key=lambda value: value.name):
        if shard.name.startswith(".tmp-"):
            temporary_count += 1
            continue
        try:
            info = shard.lstat()
        except OSError as exc:
            problems.append(
                ReceiptProblem(shard, "entry_lstat_failed", str(exc))
            )
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or not re.fullmatch(r"[0-9a-f]{2}", shard.name)
        ):
            problems.append(
                ReceiptProblem(
                    shard,
                    "unexpected_root_entry",
                    "receipt root entries must be private two-hex directories",
                )
            )
            continue
        try:
            security.require_private_directory(shard)
        except security.PrivateStateError as exc:
            problems.append(
                ReceiptProblem(shard, "unsafe_shard_permissions", str(exc))
            )
            continue
        for item in sorted(shard.iterdir(), key=lambda value: value.name):
            if item.name.startswith(".tmp-"):
                temporary_count += 1
                continue
            match = re.fullmatch(r"([0-9a-f]{64})\.json", item.name)
            try:
                info = item.lstat()
            except OSError as exc:
                problems.append(
                    ReceiptProblem(item, "entry_lstat_failed", str(exc))
                )
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or not match
                or not match.group(1).startswith(shard.name)
            ):
                problems.append(
                    ReceiptProblem(
                        item,
                        "invalid_receipt_entry",
                        "receipt must be a canonical private regular JSON file",
                    )
                )
                continue
            try:
                security.require_private_file(item)
            except security.PrivateStateError as exc:
                problems.append(
                    ReceiptProblem(item, "unsafe_receipt_permissions", str(exc))
                )
                continue
            discovered.append(item)
    return discovered, problems, temporary_count


def discover_receipts(receipt_root: Path) -> list[Path]:
    """Compatibility helper returning only isolated canonical candidates."""
    discovered, _problems, _temporary_count = scan_receipts(receipt_root)
    return discovered


def _validate_artifact_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ARTIFACT_KEYS:
        raise ConsumerError("receipt artifact fields do not match schema")
    relative = value.get("relative_path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or Path(relative).as_posix() != relative
    ):
        raise ConsumerError(f"unsafe receipt artifact path: {relative!r}")
    digest = value.get("content_sha256")
    if not isinstance(digest, str) or not EVENT_HEX.fullmatch(digest):
        raise ConsumerError(f"invalid content hash for {relative}")
    if not isinstance(value.get("byte_size"), int) or value["byte_size"] < 0:
        raise ConsumerError(f"invalid byte size for {relative}")
    if not isinstance(value.get("mtime_ns"), int) or value["mtime_ns"] < 0:
        raise ConsumerError(f"invalid mtime for {relative}")
    if not isinstance(value.get("lifecycle_hints"), list) or not all(
        isinstance(item, str) for item in value["lifecycle_hints"]
    ):
        raise ConsumerError(f"invalid lifecycle hints for {relative}")
    for field in (
        "artifact_type",
        "authority_class",
        "source_scope",
        "project",
        "artifact_id",
        "revision_id",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ConsumerError(f"invalid {field} for {relative}")
    if value.get("repository") is not None and not isinstance(
        value.get("repository"), str
    ):
        raise ConsumerError(f"invalid repository for {relative}")
    expected_graphiti = (
        "candidate"
        if value["artifact_type"] in capture.GRAPHITI_CANDIDATE_TYPES
        else "ineligible"
    )
    expected_routing = {
        "qdrant": "eligible",
        "graphiti": expected_graphiti,
        "graphiti_bulk": "disabled",
    }
    if value.get("routing") != expected_routing:
        raise ConsumerError(f"unsafe routing for {relative}")
    return value


def validate_receipt(path: Path) -> tuple[dict[str, Any], str, str]:
    receipt, receipt_sha = _read_stable_json(path)
    if set(receipt) != RECEIPT_KEYS:
        raise ConsumerError(f"receipt fields do not match schema: {path}")
    if receipt.get("schema_version") != capture.SCHEMA_VERSION:
        raise ConsumerError(f"unsupported receipt schema: {path}")
    event_hex = path.stem
    if (
        not EVENT_HEX.fullmatch(event_hex)
        or receipt.get("event_id") != f"event:{event_hex}"
    ):
        raise ConsumerError(f"receipt event identity does not match filename: {path}")
    producer = receipt.get("producer")
    if producer not in capture.PRODUCER_TYPES:
        raise ConsumerError(f"unsupported receipt producer: {producer!r}")
    if not isinstance(receipt.get("run_id"), str):
        raise ConsumerError(f"receipt run_id must be a string: {path}")
    try:
        capture.validate_run_id(str(receipt.get("run_id", "")))
    except capture.CaptureError as exc:
        raise ConsumerError(str(exc)) from exc
    try:
        datetime.fromisoformat(str(receipt.get("captured_at")))
    except ValueError as exc:
        raise ConsumerError(f"invalid captured_at in {path}") from exc
    artifacts_value = receipt.get("artifacts")
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise ConsumerError(f"receipt must contain artifacts: {path}")
    if len(artifacts_value) > MAX_RECEIPT_ARTIFACTS:
        raise ConsumerError(
            f"receipt exceeds {MAX_RECEIPT_ARTIFACTS} artifact safety limit: {path}"
        )
    artifacts = [_validate_artifact_shape(value) for value in artifacts_value]
    disallowed_types = sorted(
        {
            value["artifact_type"]
            for value in artifacts
            if value["artifact_type"] not in capture.PRODUCER_TYPES[producer]
        }
    )
    if disallowed_types:
        raise ConsumerError(
            f"{producer} receipt contains disallowed artifact types: "
            + ", ".join(disallowed_types)
        )
    paths = [value["relative_path"] for value in artifacts]
    if paths != sorted(set(paths)):
        raise ConsumerError(f"receipt artifacts must be unique and sorted: {path}")
    if receipt.get("routing_summary") != ROUTING_SUMMARY:
        raise ConsumerError(f"unsafe receipt routing summary: {path}")
    if receipt.get("safety") != capture.SAFETY:
        raise ConsumerError(f"unsafe receipt safety contract: {path}")
    expected_hex = capture.make_event_id(
        producer,
        receipt["run_id"],
        artifacts,
    )
    if expected_hex != event_hex:
        raise ConsumerError(f"receipt event digest mismatch: {path}")
    return receipt, receipt_sha, event_hex


def _expected_artifact(artifact: ingestion.CatalogArtifact) -> dict[str, Any]:
    graphiti = (
        "candidate"
        if artifact.artifact_type in capture.GRAPHITI_CANDIDATE_TYPES
        else "ineligible"
    )
    return {
        "relative_path": artifact.relative_path,
        "content_sha256": artifact.content_sha256,
        "byte_size": artifact.byte_size,
        "mtime_ns": artifact.mtime_ns,
        "artifact_type": artifact.artifact_type,
        "authority_class": artifact.authority_class,
        "lifecycle_hints": list(artifact.lifecycle_hints),
        "source_scope": artifact.source_scope,
        "repository": artifact.repository,
        "project": artifact.project,
        "artifact_id": artifact.artifact_id,
        "revision_id": artifact.revision_id,
        "routing": {
            "qdrant": "eligible",
            "graphiti": graphiti,
            "graphiti_bulk": "disabled",
        },
    }


def verify_receipt_sources(
    *,
    receipt: dict[str, Any],
    workspace: Path,
    catalog_path: Path,
) -> tuple[int, list[tuple[ingestion.CatalogArtifact, str]]]:
    catalog_run, current = ingestion.load_current_artifacts(catalog_path)
    by_path = {item.relative_path: item for item in current}
    verified: list[tuple[ingestion.CatalogArtifact, str]] = []
    for value in receipt["artifacts"]:
        relative = value["relative_path"]
        artifact = by_path.get(relative)
        if artifact is None:
            raise StaleReceiptError(
                f"receipt artifact is not current in catalog run {catalog_run}: {relative}"
            )
        if value != _expected_artifact(artifact):
            raise StaleReceiptError(
                f"receipt metadata differs from current catalog run "
                f"{catalog_run}: {relative}"
            )
        try:
            source = capture.validate_input(
                workspace / relative,
                workspace,
                "file",
            )
            text = ingestion._read_verified_source(source, artifact)
        except (capture.CaptureError, ingestion.IngestionError, OSError) as exc:
            raise StaleReceiptError(
                f"source verification failed for {relative}: {exc}"
            ) from exc
        verified.append((artifact, text))
    return catalog_run, verified


def _manifest_identity(
    *,
    event_hex: str,
    receipt_sha: str,
    catalog_run: int,
    unit_count: int,
    artifact_count: int,
    graphiti_candidates: int,
    units_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": ingestion.SCHEMA_VERSION,
        "outbox_schema_version": ingestion.OUTBOX_SCHEMA_VERSION,
        "complete": True,
        "source_mutation": "disabled",
        "sink_deletion": "disabled",
        "event_id": f"event:{event_hex}",
        "receipt_sha256": receipt_sha,
        "catalog_run_id": catalog_run,
        "graphiti_group_prefix": ingestion.DEFAULT_GRAPHITI_GROUP,
        "graphiti_write": "disabled",
        "graphiti_candidate_count": graphiti_candidates,
        "catalog_artifacts_considered": artifact_count,
        "counts": {
            "artifacts": artifact_count,
            "units": unit_count,
        },
        "chunking": {
            "max_chars": 4000,
            "overlap_chars": 400,
            "version": 1,
        },
        "units_file": "ingest-units.jsonl.gz",
        "units_sha256": units_sha,
    }


def _validate_existing_outbox(
    *,
    outbox: Path,
    expected_without_times: dict[str, Any],
) -> dict[str, Any]:
    try:
        manifest = ingestion.load_outbox_manifest(outbox)
    except (ingestion.IngestionError, OSError, json.JSONDecodeError) as exc:
        raise PoisonEventError(
            f"existing event outbox is incomplete or invalid: {outbox}: {exc}"
        ) from exc
    comparable = {
        key: manifest.get(key)
        for key in expected_without_times
    }
    if comparable != expected_without_times:
        raise PoisonEventError(
            f"existing event outbox identity differs; refusing overwrite: {outbox}"
        )
    # iter_outbox_units verifies the compressed stream and manifest checksum.
    try:
        units = sum(1 for _value in ingestion.iter_outbox_units(outbox))
    except (ingestion.IngestionError, OSError, json.JSONDecodeError) as exc:
        raise PoisonEventError(
            f"existing event outbox units are invalid: {outbox}: {exc}"
        ) from exc
    if units != int(expected_without_times["counts"]["units"]):
        raise PoisonEventError(
            f"existing event outbox unit count differs: {outbox}"
        )
    return manifest


def prepare_event_outbox(
    *,
    outbox_root: Path,
    event_hex: str,
    receipt_sha: str,
    receipt: dict[str, Any],
    catalog_run: int,
    verified: list[tuple[ingestion.CatalogArtifact, str]],
    _fault: ingestion.security.FaultHook | None = None,
) -> tuple[Path, dict[str, Any], str]:
    outbox = outbox_root / (
        f"skill-event-{event_hex}-chunks-v{ingestion.OUTBOX_SCHEMA_VERSION}"
    )
    units: list[dict[str, Any]] = []
    graphiti_candidates = 0
    for (artifact, text), receipt_artifact in zip(verified, receipt["artifacts"]):
        if receipt_artifact["routing"]["graphiti"] == "candidate":
            graphiti_candidates += 1
        for chunk in ingestion.split_text(text, max_chars=4000, overlap_chars=400):
            units.append(
                ingestion.build_unit(
                    artifact,
                    chunk,
                    catalog_run,
                    ingestion.DEFAULT_GRAPHITI_GROUP,
                )
            )
    lines = [
        (
            json.dumps(
                unit,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        for unit in units
    ]
    units_sha = hashlib.sha256(b"".join(lines)).hexdigest()
    identity = _manifest_identity(
        event_hex=event_hex,
        receipt_sha=receipt_sha,
        catalog_run=catalog_run,
        unit_count=len(units),
        artifact_count=len(verified),
        graphiti_candidates=graphiti_candidates,
        units_sha=units_sha,
    )
    if outbox.exists():
        manifest = _validate_existing_outbox(
            outbox=outbox,
            expected_without_times=identity,
        )
        return outbox, manifest, "idempotent"

    ingestion.security.ensure_private_directory(outbox_root)
    temporary = ingestion._new_outbox_temporary(outbox_root, outbox.name)
    units_path = temporary / identity["units_file"]
    try:
        with units_path.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for line in lines:
                    compressed.write(line)
            raw.flush()
            os.fsync(raw.fileno())
        ingestion.security.secure_created_file(units_path)
        if _fault is not None:
            _fault("after_units_fsync")
        started = _now()
        manifest = {
            **identity,
            "created_at": started,
            "started_at": started,
        }
        ingestion._write_manifest(temporary / "manifest.json", manifest)
        if _fault is not None:
            _fault("after_manifest_fsync")
        try:
            ingestion._publish_outbox_directory(
                temporary,
                outbox,
                fault=_fault,
            )
        except FileExistsError:
            existing = _validate_existing_outbox(
                outbox=outbox,
                expected_without_times=identity,
            )
            return outbox, existing, "idempotent"
        return outbox, manifest, "created"
    except BaseException:
        # A crash may leave only a hidden private temporary directory. It is
        # not discoverable as a published outbox and cannot wedge this event.
        raise


def _dead_letter_id(receipt_root: Path, problem: ReceiptProblem) -> str:
    try:
        relative = problem.path.absolute().relative_to(receipt_root.absolute()).as_posix()
    except ValueError:
        relative = str(problem.path.absolute())
    digest = hashlib.sha256(
        f"{problem.stage}\0{problem.code}\0{relative}".encode("utf-8")
    ).hexdigest()
    return f"dead:{digest}"


class ConsumerState:
    def __init__(self, path: Path, workspace: Path):
        security.activate_private_umask()
        self.path = _outside_workspace(path, workspace, "consumer state")
        security.ensure_private_directory(self.path.parent)
        if self.path.exists():
            security.require_private_file(self.path)
        self.connection = sqlite3.connect(self.path)
        security.secure_created_file(self.path)
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1, SCHEMA_VERSION):
            self.connection.close()
            raise ConsumerError(
                f"unsupported consumer-state schema {version}; "
                f"expected 0, 1, or {SCHEMA_VERSION}"
            )
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS consumer_events (
                event_id TEXT PRIMARY KEY,
                receipt_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('processing', 'completed', 'failed', 'stale')
                ),
                outbox_path TEXT,
                artifact_count INTEGER NOT NULL,
                unit_count INTEGER NOT NULL,
                detail TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dead_letters (
                dead_letter_id TEXT PRIMARY KEY,
                receipt_path TEXT NOT NULL,
                event_id TEXT,
                receipt_sha256 TEXT,
                stage TEXT NOT NULL,
                code TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('open', 'replay_requested', 'resolved')
                ),
                occurrences INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                resolution TEXT
            );
            CREATE INDEX IF NOT EXISTS dead_letters_status_idx
              ON dead_letters(status, last_seen_at);
            CREATE TABLE IF NOT EXISTS dead_letter_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                dead_letter_id TEXT NOT NULL REFERENCES dead_letters(dead_letter_id),
                action TEXT NOT NULL,
                detail TEXT,
                actor_uid INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconcile_jobs (
                target TEXT NOT NULL,
                catalog_run_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'completed', 'failed', 'superseded')
                ),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(target, catalog_run_id)
            );
            CREATE INDEX IF NOT EXISTS reconcile_jobs_due_idx
              ON reconcile_jobs(status, next_retry_at);
            CREATE TABLE IF NOT EXISTS health_alerts (
                fingerprint TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
                issue_codes_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL,
                resolved_at TEXT
            );
            -- Additive-only, and deliberately WITHOUT a SCHEMA_VERSION bump:
            -- older binaries sharing this DB (notably the un-synced live
            -- mirror) enforce user_version in (0, 1, 2) and would refuse to
            -- open a v3 database. CREATE IF NOT EXISTS converges the shape on
            -- every open and every read-only reader guards on table existence,
            -- so this is rollback-safe in both directions. Do not "fix" the
            -- missing bump retroactively.
            CREATE TABLE IF NOT EXISTS publication_failures (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                catalog_run_id INTEGER,
                error TEXT NOT NULL,
                first_failed_at TEXT NOT NULL,
                last_failed_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        if version == 1:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE consumer_events
                    SET status='failed',
                        detail=COALESCE(detail || '; ', '')
                          || 'recovered interrupted processing row during v2 migration',
                        updated_at=?
                    WHERE status='processing'
                    """,
                    (_now(),),
                )
        self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.connection.commit()

    def set(
        self,
        *,
        event_id: str,
        receipt_sha: str,
        status: str,
        artifact_count: int,
        unit_count: int,
        outbox: Path | None,
        detail: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO consumer_events(
                    event_id, receipt_sha256, status, outbox_path,
                    artifact_count, unit_count, detail, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    receipt_sha256=excluded.receipt_sha256,
                    status=excluded.status,
                    outbox_path=excluded.outbox_path,
                    artifact_count=excluded.artifact_count,
                    unit_count=excluded.unit_count,
                    detail=excluded.detail,
                    updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    receipt_sha,
                    status,
                    None if outbox is None else str(outbox),
                    artifact_count,
                    unit_count,
                    detail,
                    _now(),
                ),
            )

    def complete_and_schedule_reconcile(
        self,
        *,
        event_id: str,
        receipt_sha: str,
        artifact_count: int,
        unit_count: int,
        outbox: Path,
        target: str,
        catalog_run_id: int,
    ) -> None:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO consumer_events(
                    event_id, receipt_sha256, status, outbox_path,
                    artifact_count, unit_count, detail, updated_at
                ) VALUES (?, ?, 'completed', ?, ?, ?, NULL, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    receipt_sha256=excluded.receipt_sha256,
                    status='completed',
                    outbox_path=excluded.outbox_path,
                    artifact_count=excluded.artifact_count,
                    unit_count=excluded.unit_count,
                    detail=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    receipt_sha,
                    str(outbox),
                    artifact_count,
                    unit_count,
                    now,
                ),
            )
            self._schedule_reconcile_locked(target, catalog_run_id, now)

    def _schedule_reconcile_locked(
        self,
        target: str,
        catalog_run_id: int,
        now: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE reconcile_jobs
            SET status='superseded', updated_at=?
            WHERE target=? AND catalog_run_id < ?
              AND status IN ('pending', 'failed')
            """,
            (now, target, catalog_run_id),
        )
        self.connection.execute(
            """
            INSERT INTO reconcile_jobs(
              target, catalog_run_id, status, attempt_count, next_retry_at,
              last_error, created_at, updated_at, completed_at
            ) VALUES (?, ?, 'pending', 0, ?, NULL, ?, ?, NULL)
            ON CONFLICT(target, catalog_run_id) DO NOTHING
            """,
            (target, catalog_run_id, now, now, now),
        )

    def schedule_reconcile(self, target: str, catalog_run_id: int) -> None:
        now = _now()
        with self.connection:
            self._schedule_reconcile_locked(target, catalog_run_id, now)

    def due_reconcile(self, target: str) -> dict[str, Any] | None:
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            """
            SELECT target, catalog_run_id, status, attempt_count, next_retry_at,
                   last_error, created_at, updated_at, completed_at
            FROM reconcile_jobs
            WHERE target=? AND status IN ('pending', 'failed')
              AND next_retry_at <= ?
            ORDER BY catalog_run_id DESC
            LIMIT 1
            """,
            (target, _now()),
        ).fetchone()
        return None if row is None else dict(row)

    def finish_reconcile(
        self,
        *,
        target: str,
        catalog_run_id: int,
        error: str | None,
        retry_seconds: int,
    ) -> None:
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        with self.connection:
            if error is None:
                self.connection.execute(
                    """
                    UPDATE reconcile_jobs
                    SET status='completed', attempt_count=attempt_count + 1,
                        last_error=NULL, updated_at=?, completed_at=?
                    WHERE target=? AND catalog_run_id=?
                    """,
                    (now, now, target, catalog_run_id),
                )
            else:
                next_retry = (
                    now_value + timedelta(seconds=max(0, retry_seconds))
                ).isoformat()
                self.connection.execute(
                    """
                    UPDATE reconcile_jobs
                    SET status='failed', attempt_count=attempt_count + 1,
                        next_retry_at=?, last_error=?, updated_at=?,
                        completed_at=NULL
                    WHERE target=? AND catalog_run_id=?
                    """,
                    (
                        next_retry,
                        error[:2000],
                        now,
                        target,
                        catalog_run_id,
                    ),
                )

    def record_dead_letter(
        self,
        receipt_root: Path,
        problem: ReceiptProblem,
        *,
        event_id: str | None = None,
        receipt_sha: str | None = None,
    ) -> str:
        dead_id = _dead_letter_id(receipt_root, problem)
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO dead_letters(
                  dead_letter_id, receipt_path, event_id, receipt_sha256,
                  stage, code, detail, status, occurrences,
                  first_seen_at, last_seen_at, resolution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, NULL)
                ON CONFLICT(dead_letter_id) DO UPDATE SET
                  event_id=COALESCE(excluded.event_id, dead_letters.event_id),
                  receipt_sha256=COALESCE(
                    excluded.receipt_sha256, dead_letters.receipt_sha256
                  ),
                  detail=excluded.detail,
                  status='open',
                  occurrences=dead_letters.occurrences + 1,
                  last_seen_at=excluded.last_seen_at,
                  resolution=NULL
                """,
                (
                    dead_id,
                    str(problem.path.absolute()),
                    event_id,
                    receipt_sha,
                    problem.stage,
                    problem.code,
                    problem.detail[:2000],
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO dead_letter_audit(
                  dead_letter_id, action, detail, actor_uid, created_at
                ) VALUES (?, 'observed', ?, ?, ?)
                """,
                (dead_id, problem.code, platform_compat.current_uid(), now),
            )
        return dead_id

    def resolve_valid_receipt(self, path: Path) -> None:
        now = _now()
        with self.connection:
            rows = self.connection.execute(
                """
                SELECT dead_letter_id FROM dead_letters
                WHERE receipt_path=? AND status IN ('open', 'replay_requested')
                """,
                (str(path.absolute()),),
            ).fetchall()
            for (dead_id,) in rows:
                self.connection.execute(
                    """
                    UPDATE dead_letters
                    SET status='resolved', resolution='receipt validates',
                        last_seen_at=?
                    WHERE dead_letter_id=?
                    """,
                    (now, dead_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO dead_letter_audit(
                      dead_letter_id, action, detail, actor_uid, created_at
                    ) VALUES (?, 'resolved', 'receipt validates', ?, ?)
                    """,
                    (dead_id, platform_compat.current_uid(), now),
                )

    def update_health_alerts(self, issue_codes: Sequence[str]) -> tuple[str, bool]:
        normalized = sorted(set(issue_codes))
        fingerprint = hashlib.sha256(
            "\0".join(normalized).encode("utf-8")
        ).hexdigest()
        now = _now()
        transition = False
        with self.connection:
            if not normalized:
                self.connection.execute(
                    """
                    UPDATE health_alerts SET status='resolved', resolved_at=?,
                      last_seen_at=?
                    WHERE status='open'
                    """,
                    (now, now),
                )
                return fingerprint, False
            row = self.connection.execute(
                "SELECT status FROM health_alerts WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            transition = row is None or str(row[0]) != "open"
            self.connection.execute(
                """
                UPDATE health_alerts SET status='resolved', resolved_at=?,
                  last_seen_at=?
                WHERE status='open' AND fingerprint<>?
                """,
                (now, now, fingerprint),
            )
            self.connection.execute(
                """
                INSERT INTO health_alerts(
                  fingerprint, status, issue_codes_json, first_seen_at,
                  last_seen_at, occurrences, resolved_at
                ) VALUES (?, 'open', ?, ?, ?, 1, NULL)
                ON CONFLICT(fingerprint) DO UPDATE SET
                  status='open', last_seen_at=excluded.last_seen_at,
                  occurrences=health_alerts.occurrences + 1, resolved_at=NULL
                """,
                (fingerprint, json.dumps(normalized), now, now),
            )
        return fingerprint, transition

    def act_on_dead_letter(
        self,
        dead_letter_id: str,
        *,
        action: str,
        detail: str,
    ) -> None:
        if action not in {"replay", "resolve"}:
            raise ConsumerError(f"unsupported dead-letter action: {action}")
        row = self.connection.execute(
            "SELECT status FROM dead_letters WHERE dead_letter_id=?",
            (dead_letter_id,),
        ).fetchone()
        if row is None:
            raise ConsumerError(f"unknown dead-letter id: {dead_letter_id}")
        now = _now()
        next_status = "replay_requested" if action == "replay" else "resolved"
        with self.connection:
            self.connection.execute(
                """
                UPDATE dead_letters
                SET status=?, resolution=?, last_seen_at=?
                WHERE dead_letter_id=?
                """,
                (next_status, detail[:1000], now, dead_letter_id),
            )
            self.connection.execute(
                """
                INSERT INTO dead_letter_audit(
                  dead_letter_id, action, detail, actor_uid, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (dead_letter_id, action, detail[:1000], platform_compat.current_uid(), now),
            )

    def record_publication_failure(
        self, *, catalog_run_id: int | None, error: str
    ) -> None:
        """Persist an unresolved catalog-publication failure (H2).

        Durable across runs so a single failed publication cannot report a
        green consumer on the next run while catalog content sits unpublished.
        Cleared only by a later successful publication.
        """
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO publication_failures(
                    singleton, catalog_run_id, error,
                    first_failed_at, last_failed_at, attempt_count
                ) VALUES (1, ?, ?, ?, ?, 1)
                ON CONFLICT(singleton) DO UPDATE SET
                    catalog_run_id=excluded.catalog_run_id,
                    error=excluded.error,
                    last_failed_at=excluded.last_failed_at,
                    attempt_count=publication_failures.attempt_count + 1
                """,
                (catalog_run_id, error[:2000], now, now),
            )

    def clear_publication_failure(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM publication_failures")

    def has_publication_failure(self) -> bool:
        return bool(
            self.connection.execute(
                "SELECT COUNT(*) FROM publication_failures"
            ).fetchone()[0]
        )

    def publication_failure_requires_refresh(self) -> bool:
        """True when the open publication failure can ONLY clear via a rescan.

        A SourceChangedError means a source file moved relative to the catalog
        revision the publication was built from. H2's retry rebuilds off the
        LIVE catalog -- but when that catalog is the same unrefreshed run, the
        hash still disagrees and the retry is guaranteed to fail again. On a
        live vault (where the vault's own frontmatter stamper rewrites in-scope
        markdown) this loops indefinitely: observed at 18 identical attempts
        with ALL catalog content unpublished (2026-07-22).

        Reporting it as catalog staleness makes the next run rescan first,
        which is the only thing that can reconcile the hash -- and keeps the
        fail-closed guarantee intact rather than publishing unverified bytes.
        Cost note: while such a failure is open this forces a full scan per
        consumer run, so it is deliberately scoped to THIS error class.
        """
        row = self.connection.execute(
            "SELECT error FROM publication_failures LIMIT 1"
        ).fetchone()
        return row is not None and "SourceChangedError" in str(row[0])

    def close(self) -> None:
        self.connection.close()
        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if candidate.exists():
                security.secure_created_file(candidate)


def _read_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ConsumerError(f"consumer state must be a real file: {path}")
    security.require_private_file(path)
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT event_id, receipt_sha256, status, outbox_path, "
                "artifact_count, unit_count, detail, updated_at "
                "FROM consumer_events"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise ConsumerError(f"consumer state schema is unavailable: {exc}") from exc
    return {str(row["event_id"]): dict(row) for row in rows}


def _read_operational_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"dead_letters": [], "reconcile_jobs": [], "publication_failed": False}
    security.require_private_file(path)
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        dead_letters = (
            [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT dead_letter_id, stage, code, status, occurrences,
                           first_seen_at, last_seen_at, resolution
                    FROM dead_letters ORDER BY first_seen_at
                    """
                )
            ]
            if "dead_letters" in tables
            else []
        )
        reconcile_jobs = (
            [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT target, catalog_run_id, status, attempt_count,
                           next_retry_at, last_error, created_at, updated_at,
                           completed_at
                    FROM reconcile_jobs
                    ORDER BY catalog_run_id
                    """
                )
            ]
            if "reconcile_jobs" in tables
            else []
        )
        publication_failed = (
            bool(
                connection.execute(
                    "SELECT COUNT(*) FROM publication_failures"
                ).fetchone()[0]
            )
            if "publication_failures" in tables
            else False
        )
    return {
        "dead_letters": dead_letters,
        "reconcile_jobs": reconcile_jobs,
        "publication_failed": publication_failed,
    }


@dataclass(frozen=True)
class ResolvedSink:
    """The one write destination a run is permitted to publish into.

    The consumer previously took its sink from CLI flags alone, so nothing
    stopped it writing the retired embedded store while the runtime served a
    different generation.  The runtime configuration is now the authority and
    every disagreement is a refusal, never a silent fallback.
    """

    mode: str
    collection: str
    local_path: Path | None
    url: str | None
    collection_generation: str
    embedding_model: str
    embedding_model_digest: str | None
    credential: str
    resolved_from: str
    refusal: str | None
    credential_loader: Callable[[], str] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def location(self) -> str:
        if self.mode == "server":
            return f"url:{self.url}"
        return f"local:{self.local_path}"

    @property
    def identity(self) -> str | None:
        """The ingestion target identity, or None when it cannot be built."""
        try:
            # Deliberately the ingestion module's own target builder: an
            # independent copy of this format here would drift from the
            # identity ingestion actually writes under, which is the one
            # thing this field exists to report honestly.  Pinned by
            # test_sink_identity_matches_active_generation.
            return ingestion._qdrant_target(
                self.location,
                self.collection,
                self.embedding_model,
                collection_generation=self.collection_generation,
                embedding_model_digest=self.embedding_model_digest,
            )
        except Exception:
            return None

    def describe(self) -> dict[str, Any]:
        """Operator-facing sink identity.  Never includes credential bytes."""
        return {
            "mode": self.mode,
            "location": self.location,
            "collection": self.collection,
            "generation": self.collection_generation,
            "embedding_model": self.embedding_model,
            "embedding_model_digest": self.embedding_model_digest,
            "identity": self.identity,
            "credential": self.credential,
            "resolved_from": self.resolved_from,
            "refusal": self.refusal,
        }


def _build_manifest_embedding(path: Path) -> tuple[str, str]:
    """Read the frozen build manifest's embedding name and manifest digest."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsumerError(f"build manifest is unreadable: {exc}") from exc
    embedding = payload.get("embedding") if isinstance(payload, dict) else None
    if not isinstance(embedding, dict):
        raise ConsumerError("build manifest has no embedding object")
    name = embedding.get("name")
    digest = embedding.get("manifest_sha256")
    if not isinstance(name, str) or not name:
        raise ConsumerError("build manifest embedding name is invalid")
    if not isinstance(digest, str) or not DIGEST_HEX.fullmatch(digest):
        raise ConsumerError("build manifest embedding digest is invalid")
    return name, digest


def resolve_sink(args: argparse.Namespace) -> ResolvedSink:
    """Resolve the write sink from the runtime configuration, or refuse."""
    explicit_path = getattr(args, "qdrant_path", None)
    explicit_collection = getattr(args, "collection", None)
    embedding_model = getattr(
        args, "embedding_model", ingestion.DEFAULT_EMBEDDING_MODEL
    )

    if getattr(args, "no_runtime_config", False):
        # Offline/test escape hatch.  Everything must be stated explicitly so
        # this path can never be reached by accident in the installed job.
        if explicit_path is None:
            raise ConsumerError(
                "--no-runtime-config requires an explicit --qdrant-path"
            )
        return ResolvedSink(
            mode="local",
            collection=explicit_collection or ingestion.DEFAULT_COLLECTION,
            local_path=Path(explicit_path).expanduser().absolute(),
            url=None,
            collection_generation=ingestion.DEFAULT_COLLECTION_GENERATION,
            embedding_model=embedding_model,
            embedding_model_digest=None,
            credential="none",
            resolved_from="explicit-flags",
            refusal=None,
        )

    config_path = getattr(args, "runtime_config", None)
    try:
        runtime = artifact_runtime.load_runtime(
            Path(config_path) if config_path else artifact_runtime.DEFAULT_CONFIG
        )
    except (artifact_runtime.RuntimeConfigError, OSError) as exc:
        # Fail closed.  A missing or invalid runtime must never degrade into
        # writing whichever store the flags happen to name.
        raise ConsumerError(
            "runtime configuration is required to resolve a write sink "
            f"({type(exc).__name__}: {exc}); --no-runtime-config is for "
            "offline tests only"
        ) from exc

    if runtime.active_backend == "server":
        refusal: str | None = None
        if explicit_path is not None:
            refusal = (
                "local: sink refused because the runtime declares "
                "active_backend=server"
            )
        elif (
            explicit_collection is not None
            and explicit_collection != runtime.qdrant_collection
        ):
            refusal = (
                f"collection {explicit_collection!r} does not match the active "
                f"generation collection {runtime.qdrant_collection!r}"
            )
        digest: str | None = None
        if refusal is None:
            try:
                built_model, digest = _build_manifest_embedding(
                    runtime.build_manifest
                )
            except ConsumerError as exc:
                # Without the frozen digest the target identity would differ
                # from the built generation and silently re-ingest everything.
                refusal = f"server sink requires the frozen build manifest: {exc}"
            else:
                if built_model != embedding_model:
                    refusal = (
                        f"embedding model {embedding_model!r} does not match "
                        f"the built generation model {built_model!r}"
                    )
        return ResolvedSink(
            mode="server",
            collection=runtime.qdrant_collection,
            local_path=None,
            url=runtime.qdrant_url,
            collection_generation=runtime.qdrant_generation,
            embedding_model=embedding_model,
            embedding_model_digest=digest,
            credential="runtime-admin-key",
            resolved_from="runtime-config",
            refusal=refusal,
            credential_loader=runtime.qdrant_admin_key,
        )

    embedded_refusal: str | None = None
    if runtime.rollback_mode == "read-only":
        embedded_refusal = (
            "embedded sink refused because the runtime declares "
            "rollback.embedded_mode=read-only"
        )
    return ResolvedSink(
        mode="local",
        collection=explicit_collection or ingestion.DEFAULT_COLLECTION,
        local_path=(
            Path(explicit_path).expanduser().absolute()
            if explicit_path is not None
            else runtime.embedded_path
        ),
        url=None,
        collection_generation=ingestion.DEFAULT_COLLECTION_GENERATION,
        embedding_model=embedding_model,
        embedding_model_digest=None,
        credential="none",
        resolved_from="runtime-config",
        refusal=embedded_refusal,
    )


def _reconcile_target(sink: ResolvedSink) -> str:
    return f"{sink.location}|{sink.collection}"


def _catalog_generation_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "latest_attempt_run_id": None,
            "latest_attempt_status": "missing",
            "authoritative_run_id": None,
        }
    security.require_private_file(path)
    with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as connection, connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(scan_runs)")
        }
        if "status" not in columns:
            row = connection.execute(
                "SELECT MAX(run_id) FROM scan_runs WHERE finished_at IS NOT NULL"
            ).fetchone()
            run_id = None if row is None or row[0] is None else int(row[0])
            return {
                "latest_attempt_run_id": run_id,
                "latest_attempt_status": "complete" if run_id else "missing",
                "authoritative_run_id": run_id,
            }
        latest = connection.execute(
            """
            SELECT run_id, status, error FROM scan_runs
            WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1
            """
        ).fetchone()
        authoritative = connection.execute(
            """
            SELECT MAX(run_id) FROM scan_runs
            WHERE finished_at IS NOT NULL AND status='complete'
            """
        ).fetchone()
    return {
        "latest_attempt_run_id": None if latest is None else int(latest[0]),
        "latest_attempt_status": "missing" if latest is None else str(latest[1]),
        "latest_attempt_error": (
            None if latest is None or latest[2] is None else str(latest[2])
        ),
        "authoritative_run_id": (
            None
            if authoritative is None or authoritative[0] is None
            else int(authoritative[0])
        ),
    }


def _state_issue_codes(
    state: ConsumerState,
    *,
    catalog_status: dict[str, Any],
    unobserved_receipts: int,
) -> list[str]:
    issues: list[str] = []
    if catalog_status["latest_attempt_status"] != "complete":
        issues.append("catalog_generation_not_complete")
    open_dead = int(
        state.connection.execute(
            "SELECT COUNT(*) FROM dead_letters WHERE status IN ('open', 'replay_requested')"
        ).fetchone()[0]
    )
    if open_dead:
        issues.append("dead_letters_open")
    failed_events = int(
        state.connection.execute(
            "SELECT COUNT(*) FROM consumer_events WHERE status='failed'"
        ).fetchone()[0]
    )
    if failed_events:
        issues.append("event_failures")
    failed_reconcile = int(
        state.connection.execute(
            "SELECT COUNT(*) FROM reconcile_jobs WHERE status='failed'"
        ).fetchone()[0]
    )
    if failed_reconcile:
        issues.append("reconcile_failed")
    if state.has_publication_failure():
        # H2: a catalog publication that failed stays RED across runs until a
        # later publication succeeds — otherwise the consumer reports green
        # while fresh content sits in the catalog but not the serving store.
        issues.append("publication_failed")
    if unobserved_receipts:
        issues.append("unobserved_receipts")
    return issues


def _write_health(path: Path, payload: dict[str, Any]) -> None:
    security.atomic_write_json(path, payload, replace=True)


def _desktop_notify(issue_codes: Sequence[str]) -> None:
    """Best-effort local alert with no artifact content or detailed paths."""
    if not issue_codes:
        return
    message = (
        f"Artifact-memory consumer needs attention "
        f"({len(set(issue_codes))} health issue(s))."
    )
    script = (
        f'display notification "{message}" '
        'with title "workspace artifact memory"'
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


def consume(
    args: argparse.Namespace,
    *,
    qdrant_ingest: Callable[..., dict[str, Any]] = ingestion.qdrant_ingest,
    qdrant_reconcile: Callable[..., dict[str, Any]] = ingestion.qdrant_reconcile,
    notifier: Callable[[Sequence[str]], None] = _desktop_notify,
) -> tuple[dict[str, Any], bool]:
    workspace = capture.validate_workspace(args.workspace)
    receipt_root = _outside_workspace(args.receipt_root, workspace, "receipt root")
    state_path = _outside_workspace(args.state, workspace, "consumer state")
    outbox_root = _outside_workspace(args.outbox_root, workspace, "outbox root")
    catalog_path = _outside_workspace(args.catalog, workspace, "catalog")
    health_path = _outside_workspace(
        Path(
            getattr(
                args,
                "health_file",
                state_path.parent / "artifact-event-consumer-health.json",
            )
        ),
        workspace,
        "consumer health",
    )
    if int(getattr(args, "reconcile_retry_seconds", 60)) < 0:
        raise ConsumerError("--reconcile-retry-seconds must not be negative")
    receipt_paths, discovery_problems, temporary_count = scan_receipts(receipt_root)
    receipts: list[tuple[Path, dict[str, Any], str, str]] = []
    validation_problems: list[ReceiptProblem] = []
    for path in receipt_paths:
        try:
            receipt, receipt_sha, event_hex = validate_receipt(path)
        except (ConsumerError, OSError, json.JSONDecodeError) as exc:
            validation_problems.append(
                ReceiptProblem(
                    path,
                    "invalid_receipt",
                    f"{type(exc).__name__}: {exc}"[:2000],
                    "validation",
                )
            )
            continue
        receipts.append((path, receipt, receipt_sha, event_hex))
    existing = _read_state(state_path)
    pending = [
        item
        for item in receipts
        if existing.get(item[1]["event_id"], {}).get("status")
        not in {"completed", "stale"}
    ]
    # Staleness-driven refresh: receipts announce NEW artifacts, but ordinary
    # file edits produce no receipt, so a receipts-only gate lets the catalog
    # (and therefore the serving collection) fall arbitrarily far behind the
    # workspace while every run reports success. Share the watermark's clock
    # so this gate and the composite health can never disagree about "stale".
    catalog_max_age = int(
        getattr(
            args, "catalog_max_age_seconds", watermark.CATALOG_MAX_AGE_SECONDS
        )
    )
    catalog_stale = False
    if args.refresh_catalog and catalog_max_age > 0:
        component = watermark.catalog_component(catalog_path)
        age = component.get("age_seconds")
        catalog_stale = age is None or float(age) > catalog_max_age
    # Resolved before any write decision so a dry run always reports the
    # sink it WOULD have written, including when that sink is refused.
    sink = resolve_sink(args)
    if not args.apply:
        # M2: an unresolved durable publication failure makes the NEXT apply run
        # publish via the retry path even with nothing pending and a fresh
        # catalog. Without reading it here the contract above ("a dry run always
        # reports the sink it WOULD have written") is false for exactly the
        # recovery state an operator probes first. Read-only helper — no
        # credentials, plan-safe.
        publication_retry_pending = bool(
            _read_operational_state(state_path).get("publication_failed")
        )
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "plan",
                "receipt_count": len(receipts),
                "poison_count": len(discovery_problems) + len(validation_problems),
                "temporary_unpublished_count": temporary_count,
                "already_completed": len(receipts) - len(pending),
                "would_consume": len(pending),
                "would_refresh_catalog": bool(
                    args.refresh_catalog and (pending or catalog_stale)
                ),
                "would_retry_publication": publication_retry_pending,
                "qdrant_write": (
                    "refused"
                    if sink.refusal
                    else (
                        "planned"
                        if (pending or catalog_stale or publication_retry_pending)
                        else "none"
                    )
                ),
                "sink": sink.describe(),
                "graphiti_write": "disabled",
                "source_mutation": "disabled",
            },
            bool(sink.refusal),
        )

    if sink.refusal:
        # Apply mode never degrades a refusal into a warning.
        raise ConsumerError(sink.refusal)

    state = ConsumerState(state_path, workspace)
    results: list[dict[str, Any]] = []
    failed = False
    dead_letter_ids: list[str] = []
    reconcile_result: dict[str, Any] | None = None
    catalog_refresh: dict[str, Any] | None = None
    catalog_blocked = False
    target = _reconcile_target(sink)
    # Read once per run, held only for this call stack, never logged or
    # returned.  A plan run never reaches this line, so dry runs stay
    # credential-free.
    admin_key = sink.credential_loader() if sink.credential_loader else None
    try:
        for problem in [*discovery_problems, *validation_problems]:
            receipt_sha: str | None = None
            event_id: str | None = None
            if (
                problem.path.is_file()
                and EVENT_HEX.fullmatch(problem.path.stem)
            ):
                event_id = f"event:{problem.path.stem}"
                try:
                    _payload, receipt_sha = _read_stable_json(problem.path)
                except (ConsumerError, OSError, json.JSONDecodeError):
                    receipt_sha = None
            dead_letter_ids.append(
                state.record_dead_letter(
                    receipt_root,
                    problem,
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                )
            )
        for path, _receipt, _receipt_sha, _event_hex in receipts:
            state.resolve_valid_receipt(path)

        # A publication failure caused by a CHANGED SOURCE is a statement that
        # the catalog is out of date, not that the sink is unwell: retrying it
        # against the same unrefreshed catalog can never succeed. Fold it into
        # the staleness gate so the rescan that CAN clear it actually happens.
        stale_publication = state.publication_failure_requires_refresh()
        if args.refresh_catalog and (pending or catalog_stale or stale_publication):
            if catalog_path.name != "artifact-catalog.sqlite3":
                raise ConsumerError(
                    "--refresh-catalog requires a catalog named "
                    "artifact-catalog.sqlite3"
                )
            catalog_refresh = catalog.run_catalog(
                workspace,
                catalog_path.parent,
                args.policy,
                False,
            )
            if catalog_refresh["generation"]["status"] != "complete":
                catalog_blocked = True
                failed = True

        catalog_publication: dict[str, Any] | None = None
        # H1: publish whenever a refresh ADVANCED the catalog, not only when it
        # was stale BEFORE the refresh. A pending-receipt refresh also folds in
        # ordinary (receipt-less) edits that ONLY the full-catalog publication
        # ships, and it resets the catalog age so a `catalog_stale`-gated publish
        # would never fire again under active work — stranding fresh content in
        # the catalog but never in the serving collection.
        # H2: also retry a previously-failed publication even without a fresh
        # refresh, rebuilt off the live catalog, so a transient failure cannot
        # leave content permanently unpublished.
        publication_retry = state.has_publication_failure()
        if not catalog_blocked and (catalog_refresh is not None or publication_retry):
            # qdrant_reconcile is lifecycle-only: nothing else ever publishes
            # non-receipt content. Build the catalog-run outbox and let the
            # checkpointed ingest upsert only units the sink has not seen. A
            # publication failure is reported and fails the run but must not
            # block receipt processing.
            #
            # Record the attempt BEFORE starting it. Recording only in the
            # `except` leaves a crash window: a SIGKILL, power loss, or launchd
            # shutdown mid-publication records nothing, yet the refresh has
            # already reset the catalog age — so the next run sees no failure,
            # reports GREEN, and reconciles against an advanced-but-unpublished
            # catalog. That is exactly the H2 harm this milestone removes.
            # Intent-first makes the row fail-safe: success clears it; a crash
            # leaves it set so the next run retries and the reconcile guard trips.
            prepared: dict[str, Any] | None = None
            try:
                state.record_publication_failure(
                    catalog_run_id=None,
                    error="publication attempt started and did not complete",
                )
            except Exception:
                # Best-effort intent marker — a state-DB error here must not
                # escape and skip the receipt loop.
                pass
            try:
                prepared = ingestion.prepare_outbox(
                    workspace=workspace,
                    catalog=catalog_path,
                    output_root=outbox_root,
                    max_chars=5000,
                    overlap_chars=400,
                    group_id=ingestion.DEFAULT_GRAPHITI_GROUP,
                )
                catalog_publication = qdrant_ingest(
                    workspace=workspace,
                    catalog=catalog_path,
                    outbox=Path(prepared["outbox"]),
                    state_path=args.ingestion_state,
                    collection=sink.collection,
                    embedding_model=sink.embedding_model,
                    local_path=sink.local_path,
                    url=sink.url,
                    api_key_env="QDRANT_API_KEY",
                    api_key=admin_key,
                    batch_size=args.batch_size,
                    limit_units=0,
                    apply=True,
                    collection_generation=sink.collection_generation,
                    embedding_model_digest=sink.embedding_model_digest,
                )
            except Exception as exc:
                # H3: this is a DELIBERATE stage boundary. qdrant_ingest can
                # raise a raw Qdrant ApiException (UnexpectedResponse /
                # ResponseHandlingException from its pre-upsert calls), which is
                # NOT an IngestionError/OSError/sqlite3.Error; the previous
                # narrow catch let it escape consume() entirely and skip the
                # receipt loop below. Catch broadly, persist the failure (H2),
                # and continue so receipt processing is never blocked.
                # (BaseException — KeyboardInterrupt/SystemExit — still
                # propagates, by design.)
                failed = True
                catalog_publication = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                }
                try:
                    state.record_publication_failure(
                        catalog_run_id=(
                            int(prepared["catalog_run_id"])
                            if prepared
                            and prepared.get("catalog_run_id") is not None
                            else None
                        ),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    # The durable write must not itself escape and skip the
                    # receipt loop — the intent row recorded before the attempt
                    # already holds the fail-safe state, and `failed` reports it.
                    pass
            else:
                # H2: a successful publication resolves the durable failure so
                # health returns to green and reconcile resumes. Deliberately
                # OUTSIDE the guarded try: a clear that fails must not be
                # recorded as a failure of a publication that actually SUCCEEDED.
                try:
                    state.clear_publication_failure()
                except Exception as exc:
                    failed = True
                    catalog_publication = {
                        **(catalog_publication or {}),
                        "clear_failed": f"{type(exc).__name__}: {exc}"[:500],
                    }

        for path, receipt, receipt_sha, event_hex in receipts:
            event_id = receipt["event_id"]
            prior = existing.get(event_id)
            if prior and prior["receipt_sha256"] != receipt_sha:
                failed = True
                detail = "receipt bytes changed after first observation"
                dead_id = state.record_dead_letter(
                    receipt_root,
                    ReceiptProblem(
                        path,
                        "receipt_bytes_changed",
                        detail,
                        "event",
                    ),
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                )
                dead_letter_ids.append(dead_id)
                results.append(
                    {
                        "event_id": event_id,
                        "status": "dead-letter",
                        "dead_letter_id": dead_id,
                        "error": detail,
                    }
                )
                continue
            if prior and prior["status"] == "completed":
                prior_outbox = prior.get("outbox_path")
                if not isinstance(prior_outbox, str) or not prior_outbox:
                    error = PoisonEventError(
                        f"completed event has no recorded outbox: {event_id}"
                    )
                else:
                    try:
                        manifest = ingestion.load_outbox_manifest(Path(prior_outbox))
                        if (
                            manifest.get("event_id") != event_id
                            or manifest.get("receipt_sha256") != receipt_sha
                        ):
                            raise PoisonEventError(
                                f"completed event outbox identity differs: {event_id}"
                            )
                    except (
                        PoisonEventError,
                        ingestion.IngestionError,
                        OSError,
                        json.JSONDecodeError,
                    ) as exc:
                        error = PoisonEventError(str(exc))
                    else:
                        error = None
                if error is not None:
                    failed = True
                    dead_id = state.record_dead_letter(
                        receipt_root,
                        ReceiptProblem(
                            path,
                            "completed_outbox_invalid",
                            str(error),
                            "outbox",
                        ),
                        event_id=event_id,
                        receipt_sha=receipt_sha,
                    )
                    dead_letter_ids.append(dead_id)
                    results.append(
                        {
                            "event_id": event_id,
                            "status": "dead-letter",
                            "dead_letter_id": dead_id,
                            "error": str(error),
                        }
                    )
                    continue
                results.append({"event_id": event_id, "status": "already-completed"})
                continue
            if prior and prior["status"] == "stale":
                results.append({"event_id": event_id, "status": "already-stale"})
                continue
            if catalog_blocked:
                results.append(
                    {
                        "event_id": event_id,
                        "status": "deferred",
                        "error": "catalog refresh did not produce a complete generation",
                    }
                )
                continue
            outbox: Path | None = None
            unit_count = 0
            try:
                catalog_run, verified = verify_receipt_sources(
                    receipt=receipt,
                    workspace=workspace,
                    catalog_path=catalog_path,
                )
                outbox, manifest, outbox_status = prepare_event_outbox(
                    outbox_root=outbox_root,
                    event_hex=event_hex,
                    receipt_sha=receipt_sha,
                    receipt=receipt,
                    catalog_run=catalog_run,
                    verified=verified,
                )
                unit_count = int(manifest["counts"]["units"])
                state.set(
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                    status="processing",
                    artifact_count=len(verified),
                    unit_count=unit_count,
                    outbox=outbox,
                )
                qdrant = qdrant_ingest(
                    workspace=workspace,
                    catalog=catalog_path,
                    outbox=outbox,
                    state_path=args.ingestion_state,
                    collection=sink.collection,
                    embedding_model=sink.embedding_model,
                    local_path=sink.local_path,
                    url=sink.url,
                    api_key_env="QDRANT_API_KEY",
                    api_key=admin_key,
                    batch_size=args.batch_size,
                    limit_units=0,
                    apply=True,
                    collection_generation=sink.collection_generation,
                    embedding_model_digest=sink.embedding_model_digest,
                )
                state.complete_and_schedule_reconcile(
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                    artifact_count=len(verified),
                    unit_count=unit_count,
                    outbox=outbox,
                    target=target,
                    catalog_run_id=catalog_run,
                )
                results.append(
                    {
                        "event_id": event_id,
                        "status": "completed",
                        "outbox_status": outbox_status,
                        "outbox": str(outbox),
                        "artifacts": len(verified),
                        "units": unit_count,
                        "graphiti_candidates": manifest["graphiti_candidate_count"],
                        "qdrant": qdrant,
                        "graphiti_write": "disabled",
                    }
                )
            except PoisonEventError as exc:
                failed = True
                text = f"{type(exc).__name__}: {exc}"[:2000]
                dead_id = state.record_dead_letter(
                    receipt_root,
                    ReceiptProblem(
                        path,
                        "poison_event",
                        text,
                        "outbox",
                    ),
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                )
                dead_letter_ids.append(dead_id)
                results.append(
                    {
                        "event_id": event_id,
                        "status": "dead-letter",
                        "dead_letter_id": dead_id,
                        "error": text,
                    }
                )
            except StaleReceiptError as exc:
                failed = True
                text = f"{type(exc).__name__}: {exc}"[:2000]
                state.set(
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                    status="stale",
                    artifact_count=len(receipt["artifacts"]),
                    unit_count=unit_count,
                    outbox=outbox,
                    detail=text,
                )
                results.append(
                    {"event_id": event_id, "status": "stale", "error": text}
                )
            except Exception as exc:
                failed = True
                text = f"{type(exc).__name__}: {exc}"[:2000]
                state.set(
                    event_id=event_id,
                    receipt_sha=receipt_sha,
                    status="failed",
                    artifact_count=len(receipt["artifacts"]),
                    unit_count=unit_count,
                    outbox=outbox,
                    detail=text,
                )
                results.append(
                    {"event_id": event_id, "status": "failed", "error": text}
                )

        try:
            latest_catalog_run, _current = ingestion.load_current_artifacts(
                catalog_path
            )
            state.schedule_reconcile(target, latest_catalog_run)
        except (ingestion.IngestionError, OSError, sqlite3.Error) as exc:
            failed = True
            reconcile_result = {
                "status": "catalog-unavailable",
                "error": f"{type(exc).__name__}: {exc}"[:2000],
                "point_deletion": "disabled",
            }

        if reconcile_result is None and state.has_publication_failure():
            # H2: do not reconcile lifecycle against a catalog whose content is
            # not fully published — marking unpublished revisions current (and
            # their predecessors historical) would take live points dark. The
            # scheduled job stays pending and runs once publication succeeds.
            reconcile_result = {
                "status": "skipped-unpublished-catalog",
                "point_deletion": "disabled",
            }
        due = state.due_reconcile(target)
        if due is not None and reconcile_result is None:
            try:
                reconcile_result = qdrant_reconcile(
                    workspace=workspace,
                    catalog=catalog_path,
                    collection=sink.collection,
                    local_path=sink.local_path,
                    url=sink.url,
                    api_key_env="QDRANT_API_KEY",
                    api_key=admin_key,
                    batch_size=max(args.batch_size, 32),
                    apply=True,
                )
                state.finish_reconcile(
                    target=target,
                    catalog_run_id=int(due["catalog_run_id"]),
                    error=None,
                    retry_seconds=int(
                        getattr(args, "reconcile_retry_seconds", 60)
                    ),
                )
            except Exception as exc:
                failed = True
                error = f"{type(exc).__name__}: {exc}"[:2000]
                state.finish_reconcile(
                    target=target,
                    catalog_run_id=int(due["catalog_run_id"]),
                    error=error,
                    retry_seconds=int(
                        getattr(args, "reconcile_retry_seconds", 60)
                    ),
                )
                reconcile_result = {
                    "status": "error",
                    "catalog_run_id": int(due["catalog_run_id"]),
                    "error": error,
                    "point_deletion": "disabled",
                }
        elif due is None and reconcile_result is None:
            reconcile_result = {
                "status": "not-due",
                "point_deletion": "disabled",
            }

        current_state = _read_state(state_path)
        known_receipts = {item[1]["event_id"] for item in receipts}
        unobserved = len(known_receipts - set(current_state))
        catalog_status = _catalog_generation_status(catalog_path)
        issue_codes = _state_issue_codes(
            state,
            catalog_status=catalog_status,
            unobserved_receipts=unobserved,
        )
        fingerprint, transition = state.update_health_alerts(issue_codes)
        health = {
            "schema_version": SCHEMA_VERSION,
            "observed_at": _now(),
            "healthy": not issue_codes,
            "issue_codes": issue_codes,
            "fingerprint": fingerprint,
            "catalog": catalog_status,
            "receipt_count": len(receipts),
            "unobserved_receipts": unobserved,
            "dead_letter_count": int(
                state.connection.execute(
                    "SELECT COUNT(*) FROM dead_letters "
                    "WHERE status IN ('open', 'replay_requested')"
                ).fetchone()[0]
            ),
            "failed_event_count": int(
                state.connection.execute(
                    "SELECT COUNT(*) FROM consumer_events WHERE status='failed'"
                ).fetchone()[0]
            ),
            "failed_reconcile_count": int(
                state.connection.execute(
                    "SELECT COUNT(*) FROM reconcile_jobs WHERE status='failed'"
                ).fetchone()[0]
            ),
        }
        _write_health(
            health_path,
            health,
        )
        if transition and bool(getattr(args, "desktop_notify", False)):
            try:
                notifier(issue_codes)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "notification-error",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    ),
                    file=sys.stderr,
                )
        failed = failed or bool(issue_codes)
    finally:
        state.close()
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "receipt_count": len(receipts),
            "processed": len(results),
            "poison_count": len(discovery_problems) + len(validation_problems),
            "temporary_unpublished_count": temporary_count,
            "dead_letter_ids": sorted(set(dead_letter_ids)),
            "failed": sum(
                1
                for item in results
                if item["status"] in {"failed", "stale", "dead-letter"}
            ),
            "results": results,
            "catalog_refresh": catalog_refresh,
            "catalog_publication": catalog_publication,
            "qdrant_reconcile": reconcile_result,
            "qdrant_deletion": "disabled",
            "graphiti_write": "disabled",
            "source_mutation": "disabled",
        },
        failed,
    )


def status(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    workspace = capture.validate_workspace(args.workspace)
    receipt_root = _outside_workspace(args.receipt_root, workspace, "receipt root")
    state_path = _outside_workspace(args.state, workspace, "consumer state")
    receipts, discovery_problems, temporary_count = scan_receipts(receipt_root)
    valid_receipts: list[Path] = []
    validation_problems: list[ReceiptProblem] = []
    for path in receipts:
        try:
            validate_receipt(path)
        except (ConsumerError, OSError, json.JSONDecodeError) as exc:
            validation_problems.append(
                ReceiptProblem(
                    path,
                    "invalid_receipt",
                    f"{type(exc).__name__}: {exc}"[:2000],
                    "validation",
                )
            )
        else:
            valid_receipts.append(path)
    state = _read_state(state_path)
    operational = _read_operational_state(state_path)
    counts = Counter(str(value["status"]) for value in state.values())
    known_receipts = {f"event:{path.stem}" for path in valid_receipts}
    open_dead = [
        value
        for value in operational["dead_letters"]
        if value["status"] in {"open", "replay_requested"}
    ]
    failed_reconcile = [
        value
        for value in operational["reconcile_jobs"]
        if value["status"] == "failed"
    ]
    catalog_path = Path(
        getattr(args, "catalog", ingestion.DEFAULT_CATALOG)
    )
    catalog_status = _catalog_generation_status(catalog_path)
    quarantine = watermark.outbox_quarantine_component(
        Path(getattr(args, "dead_letter_root", DEFAULT_DEAD_LETTER_ROOT))
    )
    poison_count = len(discovery_problems) + len(validation_problems)
    unobserved = len(known_receipts - set(state))
    failed = bool(
        poison_count
        or open_dead
        or failed_reconcile
        or counts.get("failed", 0)
        or unobserved
        # Only OPEN quarantine items degrade health; acknowledged retained
        # residue stays visible in the payload without a forever-red status.
        or quarantine.get("open", quarantine["count"])
        # H5: ...but a quarantine surface we cannot READ is not "zero open".
        # The early return on an unreadable root leaves open/count at 0 with
        # state=unknown, which previously reported healthy — a can't-tell
        # reported as an all-clear. Any failing component state now fails.
        or quarantine.get("state") in watermark.FAILING_STATES
        or catalog_status["latest_attempt_status"] != "complete"
        # H2: a durable catalog-publication failure keeps status red until a
        # later publication succeeds — content is in the catalog, not the store.
        or operational["publication_failed"]
    )
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "status",
            "receipt_count": len(valid_receipts),
            "poison_count": poison_count,
            "temporary_unpublished_count": temporary_count,
            "unobserved_receipts": unobserved,
            "state_event_count": len(state),
            "by_status": dict(sorted(counts.items())),
            "dead_letters": {
                "open": len(open_dead),
                "total": len(operational["dead_letters"]),
                "records": operational["dead_letters"],
            },
            # A quarantined outbox DIRECTORY is a different object from a
            # dead-letter DB record, and it appeared in no status output at
            # all — so an operator auditing dead letters could read total=0
            # while a quarantined outbox sat on disk. Both are surfaced now.
            "outbox_quarantine": quarantine,
            "reconcile_jobs": operational["reconcile_jobs"],
            "publication_failed": operational["publication_failed"],
            "catalog": catalog_status,
            "healthy": not failed,
            "graphiti_write": "disabled",
        },
        failed,
    )


def dead_letter_list(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    workspace = capture.validate_workspace(args.workspace)
    state_path = _outside_workspace(args.state, workspace, "consumer state")
    operational = _read_operational_state(state_path)
    records = operational["dead_letters"]
    if getattr(args, "open_only", False):
        records = [
            value
            for value in records
            if value["status"] in {"open", "replay_requested"}
        ]
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "dead-letter-list",
            "count": len(records),
            "records": records,
        },
        False,
    )


def dead_letter_action(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    workspace = capture.validate_workspace(args.workspace)
    state_path = _outside_workspace(args.state, workspace, "consumer state")
    operational = _read_operational_state(state_path)
    matches = [
        value
        for value in operational["dead_letters"]
        if value["dead_letter_id"] == args.dead_letter_id
    ]
    if not matches:
        raise ConsumerError(f"unknown dead-letter id: {args.dead_letter_id}")
    if not args.apply:
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "plan",
                "would": args.dead_letter_action,
                "dead_letter_id": args.dead_letter_id,
                "source_mutation": "none",
                "sink_writes": "none",
            },
            False,
        )
    record = matches[0]
    if args.dead_letter_action == "replay":
        # Replay never edits receipt bytes. It is allowed only after the
        # operator has restored a canonical, fully validating receipt.
        with closing(sqlite3.connect(
            f"file:{state_path.resolve()}?mode=ro", uri=True
        )) as connection, connection:
            row = connection.execute(
                "SELECT receipt_path FROM dead_letters WHERE dead_letter_id=?",
                (args.dead_letter_id,),
            ).fetchone()
        if row is None:
            raise ConsumerError(f"unknown dead-letter id: {args.dead_letter_id}")
        validate_receipt(Path(str(row[0])))
    state = ConsumerState(state_path, workspace)
    try:
        state.act_on_dead_letter(
            args.dead_letter_id,
            action=args.dead_letter_action,
            detail=args.resolution,
        )
    finally:
        state.close()
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "status": (
                "replay-requested"
                if args.dead_letter_action == "replay"
                else "resolved"
            ),
            "dead_letter_id": record["dead_letter_id"],
            "audit": "recorded",
        },
        False,
    )


def supervisor_event(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    """Append one immutable record of a supervisor state change.

    The consumer agent's own unload was an unrecorded state change: nothing
    said when it left launchctl or why, so an operator could not tell a
    deliberate protective act from a silent failure.  Supervisor transitions
    now leave an append-only trace.  This log is NOT the receipt root -- these
    are audit records about the supervisor, not artifact publications, and
    feeding them to the receipt scanner would conflate the two.
    """
    workspace = capture.validate_workspace(args.workspace)
    log_path = _outside_workspace(
        Path(getattr(args, "supervisor_log", DEFAULT_SUPERVISOR_LOG)),
        workspace,
        "supervisor event log",
    )
    definition = Path(args.definition).expanduser() if args.definition else None
    definition_sha: str | None = None
    if definition is not None:
        try:
            definition_sha = hashlib.sha256(definition.read_bytes()).hexdigest()
        except OSError as exc:
            raise ConsumerError(f"supervisor definition is unreadable: {exc}") from exc
    record = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": _now(),
        "label": args.label,
        "transition": args.transition,
        "reason": str(args.reason)[:2000],
        "actor": args.actor,
        # Binds the record to the exact supervisor definition in force, so a
        # later reader can tell which sink the job would have written.
        "definition": str(definition) if definition else None,
        "definition_sha256": definition_sha,
    }
    if not args.apply:
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "plan",
                "would_append": record,
                "log": str(log_path),
            },
            False,
        )
    line = json.dumps(record, sort_keys=True) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # O_APPEND so concurrent writers cannot interleave or truncate; the log is
    # never opened for writing in any other mode.
    handle = os.open(
        log_path, os.O_WRONLY | getattr(os, "O_BINARY", 0) | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        os.write(handle, line.encode("utf-8"))
    finally:
        os.close(handle)
    os.chmod(log_path, 0o600)
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "appended": record,
            "log": str(log_path),
        },
        False,
    )


def quarantine_ack(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    """Acknowledge one quarantined outbox directory as retained residue.

    The acknowledgment is content-bound (a v2 fingerprint over file CONTENT,
    entry types and symlink targets) and time-bound: --review-by is REQUIRED
    and must be a future instant, because an acknowledgment that can never age
    out would suppress the entry forever. A changed directory, a passed review
    date, or a missing/unparseable one all reopen the entry.

    --apply requires --expected-fingerprint (run the plan first and pass back
    what it reports) so the approval is bound to the reviewed state, and
    superseding an acknowledgment that covers different content requires
    --replace, which records a `supersedes` trail rather than overwriting.

    The directory itself is never modified or deleted — disposition of the
    bytes remains a separately approved action.
    """
    workspace = capture.validate_workspace(args.workspace)
    root = _outside_workspace(
        Path(args.dead_letter_root), workspace, "quarantine root"
    )
    ledger_path = (
        _outside_workspace(Path(args.ack_ledger), workspace, "ack ledger")
        if getattr(args, "ack_ledger", None)
        else root.parent / watermark.QUARANTINE_ACK_FILENAME
    )
    review_by = getattr(args, "review_by", None)
    if not review_by:
        raise ConsumerError(
            "--review-by is required: an acknowledgment with no review date "
            "can never age out, so it would suppress the entry forever"
        )
    try:
        review_at = datetime.fromisoformat(review_by)
    except ValueError as exc:
        raise ConsumerError("--review-by must be an ISO-8601 timestamp") from exc
    if review_at.tzinfo is None:
        review_at = review_at.replace(tzinfo=timezone.utc)
    if review_at <= datetime.now(timezone.utc):
        raise ConsumerError(
            f"--review-by must be in the FUTURE; {review_by!r} has already "
            "passed and the acknowledgment would reopen immediately"
        )
    # Containment: --name is a single directory name directly under the
    # quarantine root, never a path. A bare join is NOT a boundary —
    # `root / "/etc"` yields "/etc" and `root / ".."` escapes upward.
    name = str(args.name)
    if (
        name in {"", ".", ".."}
        or "/" in name
        or "\\" in name
        # A NUL would otherwise reach target.resolve() and raise a raw
        # ValueError traceback instead of the structured refusal every other
        # hostile input gets.
        or "\x00" in name
    ):
        raise ConsumerError(
            f"--name must be a single directory name directly under {root}, "
            f"not a path: {name!r}"
        )
    target = root / name
    if target.resolve().parent != root.resolve():
        raise ConsumerError(
            f"--name resolves outside the quarantine root: {name!r}"
        )
    if target.is_symlink() or not target.is_dir():
        raise ConsumerError(
            f"no quarantined directory named {name!r} under {root}"
        )
    fingerprint = watermark.quarantine_fingerprint(target)
    if fingerprint is None:
        raise ConsumerError("quarantined directory could not be fingerprinted")
    # Bind apply to what the operator actually reviewed: plan returns this
    # fingerprint, and passing it back refuses if the directory changed in
    # between (plan and apply otherwise fingerprint current state twice).
    expected = getattr(args, "expected_fingerprint", None)
    if args.apply and not expected:
        # M3: an opt-in binding leaves the unbound path as the path of least
        # resistance, so the review-time/apply-time TOCTOU stays open by
        # default. Plan first, then pass back what you actually reviewed.
        raise ConsumerError(
            "--expected-fingerprint is required with --apply: run the plan "
            "first and pass back the fingerprint it reports, so the "
            "acknowledgment is bound to the state you actually reviewed"
        )
    if expected and expected != fingerprint:
        raise ConsumerError(
            "quarantined directory changed since it was reviewed: expected "
            f"{expected[:16]}..., found {fingerprint[:16]}... — re-run the plan"
        )
    entry = {
        "name": name,
        "fingerprint": fingerprint,
        "acknowledged_at": _now(),
        "actor_uid": platform_compat.current_uid(),
        "reason": args.reason,
        "review_by": review_by,
    }
    if not args.apply:
        # Surface the exact apply invocation: the required flags otherwise
        # exist only in error messages, so the contract is undiscoverable.
        existing = watermark.load_quarantine_acknowledgments(ledger_path).get(name)
        requires_replace = (
            existing is not None and existing.get("fingerprint") != fingerprint
        )
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "plan",
                "would_acknowledge": entry,
                "ledger": str(ledger_path),
                "requires_replace": requires_replace,
                "suggested_apply": (
                    f"--name {name} --review-by {review_by}"
                    f" --expected-fingerprint {fingerprint}"
                    + (" --replace" if requires_replace else "")
                    + " --apply"
                ),
            },
            False,
        )
    ledger = watermark.load_quarantine_acknowledgments(ledger_path)
    # V-M1: load_quarantine_acknowledgments fails OPEN to {} on an unreadable
    # or corrupt ledger. That is correct for the read/health path (every entry
    # then reads as open) but DESTRUCTIVE here: this function rewrites the
    # whole ledger, so an empty load would silently delete every OTHER
    # acknowledgment and bypass the --replace gate for this one. Distinguish a
    # legitimately empty ledger from an unparseable one and refuse the latter.
    if not ledger and ledger_path.exists():
        try:
            raw_payload = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConsumerError(
                f"acknowledgment ledger exists but could not be read/parsed "
                f"({type(exc).__name__}); refusing to rewrite it and lose "
                "existing acknowledgments — inspect or restore it first"
            ) from exc
        if not isinstance(raw_payload, dict) or not isinstance(
            raw_payload.get("entries"), list
        ):
            raise ConsumerError(
                "acknowledgment ledger exists but carries no valid 'entries' "
                "list; refusing to rewrite it and lose existing "
                "acknowledgments — inspect or restore it first"
            )
    previous = ledger.get(name)
    if (
        previous is not None
        and previous.get("fingerprint") != fingerprint
        and not getattr(args, "replace", False)
    ):
        # A pre-v2 record (no actor_uid) reopens because the fingerprint
        # ALGORITHM changed, not necessarily because the bytes did — saying
        # "DIFFERENT content" there reads like tampering and misdirects triage.
        legacy_hint = (
            " NOTE: the stored record predates the v2 content-hashed"
            " fingerprint, so the algorithm changed and the content may be"
            " unchanged — re-review, then re-acknowledge."
            if "actor_uid" not in previous
            else ""
        )
        raise ConsumerError(
            f"{name!r} already carries an acknowledgment for DIFFERENT content "
            f"— re-review it and pass --replace to supersede that approval.{legacy_hint}"
        )
    if previous is not None:
        # Keep a supersession trail instead of a silent last-writer-wins
        # overwrite, so an approval's lineage survives in the ledger.
        entry["supersedes"] = {
            "fingerprint": previous.get("fingerprint"),
            "acknowledged_at": previous.get("acknowledged_at"),
            "actor_uid": previous.get("actor_uid"),
        }
    ledger[name] = entry
    payload = {
        "schema_version": 1,
        "entries": [ledger[name] for name in sorted(ledger)],
    }
    security.atomic_write_json(ledger_path, payload, replace=ledger_path.exists())
    component = watermark.outbox_quarantine_component(root, ledger_path)
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "acknowledged": entry,
            "ledger": str(ledger_path),
            "quarantine": {
                "count": component["count"],
                "open": component["open"],
                "state": component["state"],
            },
        },
        False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    common.add_argument("--receipt-root", type=Path, default=DEFAULT_RECEIPT_ROOT)
    common.add_argument("--state", type=Path, default=DEFAULT_STATE)

    consume_parser = subparsers.add_parser("consume", parents=[common])
    consume_parser.add_argument("--catalog", type=Path, default=ingestion.DEFAULT_CATALOG)
    consume_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    consume_parser.add_argument("--outbox-root", type=Path, default=DEFAULT_OUTBOX_ROOT)
    consume_parser.add_argument(
        "--ingestion-state",
        type=Path,
        default=ingestion.DEFAULT_STATE,
    )
    # Defaults are None so resolve_sink can tell "the operator named this
    # sink" from "nobody said anything".  The runtime configuration supplies
    # the real values; naming one that disagrees with it is a refusal.
    consume_parser.add_argument(
        "--qdrant-path",
        type=Path,
        default=None,
    )
    consume_parser.add_argument(
        "--collection",
        default=None,
    )
    consume_parser.add_argument(
        "--runtime-config",
        type=Path,
        default=None,
        help="artifact-memory runtime config (default: the installed one)",
    )
    consume_parser.add_argument(
        "--no-runtime-config",
        action="store_true",
        help="offline tests only: require an explicit --qdrant-path sink",
    )
    consume_parser.add_argument(
        "--embedding-model",
        default=ingestion.DEFAULT_EMBEDDING_MODEL,
    )
    consume_parser.add_argument("--batch-size", type=int, default=32)
    consume_parser.add_argument("--refresh-catalog", action="store_true")
    consume_parser.add_argument(
        "--catalog-max-age-seconds",
        type=int,
        default=watermark.CATALOG_MAX_AGE_SECONDS,
        help=(
            "with --refresh-catalog, also refresh when the newest complete "
            "catalog run is older than this many seconds even if no receipts "
            "are pending (0 disables staleness-driven refresh)"
        ),
    )
    consume_parser.add_argument(
        "--health-file",
        type=Path,
        default=DEFAULT_HEALTH,
    )
    consume_parser.add_argument(
        "--reconcile-retry-seconds",
        type=int,
        default=60,
    )
    consume_parser.add_argument("--desktop-notify", action="store_true")
    consume_parser.add_argument("--apply", action="store_true")
    consume_parser.set_defaults(handler=consume)

    status_parser = subparsers.add_parser("status", parents=[common])
    status_parser.add_argument(
        "--catalog",
        type=Path,
        default=ingestion.DEFAULT_CATALOG,
    )
    status_parser.add_argument(
        "--dead-letter-root",
        type=Path,
        default=DEFAULT_DEAD_LETTER_ROOT,
        help="quarantined outbox directories surfaced alongside dead-letter records",
    )
    status_parser.set_defaults(handler=status)

    dead_list = subparsers.add_parser("dead-letter-list", parents=[common])
    dead_list.add_argument("--open-only", action="store_true")
    dead_list.set_defaults(handler=dead_letter_list)

    dead_action = subparsers.add_parser("dead-letter", parents=[common])
    dead_action.add_argument(
        "dead_letter_action",
        choices=("replay", "resolve"),
    )
    dead_action.add_argument(
        "--id",
        dest="dead_letter_id",
        required=True,
    )
    dead_action.add_argument("--resolution", required=True)
    dead_action.add_argument("--apply", action="store_true")
    dead_action.set_defaults(handler=dead_letter_action)

    ack_parser = subparsers.add_parser("quarantine-ack", parents=[common])
    ack_parser.add_argument(
        "--dead-letter-root", type=Path, default=DEFAULT_DEAD_LETTER_ROOT
    )
    ack_parser.add_argument("--ack-ledger", type=Path, default=None)
    ack_parser.add_argument("--name", required=True)
    ack_parser.add_argument("--reason", required=True)
    # Required: an acknowledgment with no review date can never age out.
    ack_parser.add_argument("--review-by", required=True)
    # Binds apply to the state plan showed; refuses if it changed in between.
    ack_parser.add_argument("--expected-fingerprint", default=None)
    # Required to supersede an existing acknowledgment of DIFFERENT content.
    ack_parser.add_argument("--replace", action="store_true")
    ack_parser.add_argument("--apply", action="store_true")
    ack_parser.set_defaults(handler=quarantine_ack)

    supervisor = subparsers.add_parser("supervisor-event", parents=[common])
    supervisor.add_argument("--label", required=True)
    supervisor.add_argument(
        "--transition",
        required=True,
        choices=("loaded", "unloaded", "redefined", "observed-absent"),
    )
    supervisor.add_argument("--reason", required=True)
    supervisor.add_argument("--actor", default="builder-session")
    supervisor.add_argument("--definition", type=Path, default=None)
    supervisor.add_argument(
        "--supervisor-log",
        type=Path,
        default=DEFAULT_SUPERVISOR_LOG,
    )
    supervisor.add_argument("--apply", action="store_true")
    supervisor.set_defaults(handler=supervisor_event)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result, failed = args.handler(args)
    except (
        ConsumerError,
        capture.CaptureError,
        catalog.PolicyError,
        catalog.ChangedDuringScan,
        ingestion.IngestionError,
        OSError,
        sqlite3.Error,
    ) as exc:
        if args.command == "consume" and getattr(args, "apply", False):
            try:
                _write_health(
                    Path(getattr(args, "health_file", DEFAULT_HEALTH)),
                    {
                        "schema_version": SCHEMA_VERSION,
                        "observed_at": _now(),
                        "healthy": False,
                        "issue_codes": ["fatal_consumer_error"],
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    },
                )
            except Exception:
                pass
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "status": "error", "error": str(exc)}
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
