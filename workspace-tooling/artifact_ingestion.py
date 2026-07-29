#!/usr/bin/env python3
"""Prepare and ingest workspace artifacts into Qdrant and Graphiti.

The preparation path is stdlib-only and source-read-only. It consumes the
Phase-1 SQLite catalog, verifies that every current source still matches its
cataloged revision, and writes a deterministic compressed outbox outside the
workspace.

Live sinks are explicit:

* Qdrant uses deterministic point UUIDs and idempotent upserts.
* Graphiti is FalkorDB-only and uses a durable checkpoint before each episode.
  Ambiguous attempts are never retried automatically.

No command deletes source files, Qdrant points, Graphiti nodes, or outboxes.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import stat
import sys
import uuid
from urllib.parse import urlparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import graphiti_policy
import artifact_security as security
import artifact_runtime


SCHEMA_VERSION = 1
OUTBOX_SCHEMA_VERSION = 2
SUPPORTED_OUTBOX_SCHEMA_VERSIONS = (1, 2)
STATE_SCHEMA_VERSION = 1
DEFAULT_DERIVED_ROOT = artifact_runtime.derived_root()
DEFAULT_CATALOG = DEFAULT_DERIVED_ROOT / "artifact-catalog.sqlite3"
DEFAULT_STATE = DEFAULT_DERIVED_ROOT / "ingestion-state.sqlite3"
DEFAULT_QDRANT_PATH = DEFAULT_DERIVED_ROOT / "qdrant"
DEFAULT_MODEL_CACHE = DEFAULT_DERIVED_ROOT / "model-cache"
DEFAULT_COLLECTION = "personal_artifact_chunks_v1"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_CHUNK_PROFILE_ID = "markdown-heading-char-v1"
DEFAULT_NORMALIZATION_VERSION = "utf8-replace-strip-v1"
DEFAULT_COLLECTION_GENERATION = "embedded-v1"
DEFAULT_GRAPHITI_GROUP = "personal_artifacts"
DEFAULT_GRAPHITI_ARTIFACT_TYPES = ("decision", "handoff", "plan", "roadmap")
MAX_GRAPHITI_PILOT_UNITS = 1
DEFAULT_GRAPHITI_INSTRUCTIONS = (
    "Extract only facts explicitly stated in the episode about decisions, "
    "status changes, ownership, dependencies, milestones, deadlines, and "
    "supersession. Preserve stated dates and uncertainty. Do not invent "
    "causal links or generic RELATED_TO-style relationships. Do not create "
    "entities for configuration keys, file paths, commands, or example "
    "credentials unless the episode explicitly changes their lifecycle or "
    "assigns responsibility for them."
)
QDRANT_PAYLOAD_INDEXES = {
    "revision_id": "keyword",
    "artifact_id": "keyword",
    "content_sha256": "keyword",
    "chunk_sha256": "keyword",
    "project": "keyword",
    "artifact_type": "keyword",
    "authority_class": "keyword",
    "source_scope": "keyword",
    "repository": "keyword",
    "lifecycle_hints": "keyword",
    "catalog_current": "bool",
    "chunk_profile_id": "keyword",
    "normalization_version": "keyword",
    "collection_generation": "keyword",
}
POINT_NAMESPACE = uuid.UUID("10e52e2b-f43d-5e35-875b-cf70c47e5bbf")
UNIT_NAMESPACE = uuid.UUID("0a410ff4-012e-5908-9317-f7529b1d8753")
EPISODE_KEY_NAMESPACE = uuid.UUID("dad68f71-831c-51a4-aa31-b9c697db6d1c")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^[ \t]*(```+|~~~+)")
GROUP_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class IngestionError(ValueError):
    """The requested ingestion operation cannot be completed safely."""


class SourceChangedError(IngestionError):
    """A source no longer matches the catalog revision."""


@dataclass(frozen=True)
class CatalogArtifact:
    artifact_id: str
    revision_id: str
    relative_path: str
    content_sha256: str
    byte_size: int
    mtime_ns: int
    artifact_type: str
    authority_class: str
    lifecycle_hints: tuple[str, ...]
    source_scope: str
    repository: str | None
    project: str


@dataclass(frozen=True)
class TextChunk:
    index: int
    content: str
    embedding_text: str
    overlap_prefix_chars: int
    heading: str | None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _outside_workspace(path: Path, workspace: Path, label: str) -> Path:
    resolved = path.expanduser().absolute().resolve(strict=False)
    if _inside(resolved, workspace):
        raise IngestionError(f"{label} must be outside the source workspace: {resolved}")
    return resolved


def _latest_catalog_run(connection: sqlite3.Connection) -> int:
    columns = {
        str(value[1])
        for value in connection.execute("PRAGMA table_info(scan_runs)")
    }
    if "status" in columns:
        row = connection.execute(
            "SELECT MAX(run_id) FROM scan_runs "
            "WHERE finished_at IS NOT NULL AND status='complete'"
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT MAX(run_id) FROM scan_runs WHERE finished_at IS NOT NULL"
        ).fetchone()
    if row is None or row[0] is None:
        raise IngestionError("catalog contains no completed scan")
    return int(row[0])


def load_current_artifacts(catalog: Path) -> tuple[int, list[CatalogArtifact]]:
    catalog = catalog.expanduser().resolve(strict=True)
    security.require_private_file(catalog)
    with sqlite3.connect(catalog) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (2, 3):
            raise IngestionError(
                f"unsupported Phase-1 catalog schema {version}; expected 2 or 3"
            )
        run_id = _latest_catalog_run(connection)
        rows = connection.execute(
            """
            SELECT artifact_id, revision_id, relative_path, content_sha256,
                   byte_size, mtime_ns, artifact_type, authority_class,
                   lifecycle_hints_json, source_scope, repository, project
            FROM current_artifact_revisions
            ORDER BY mtime_ns, relative_path
            """
        ).fetchall()
    artifacts = [
        CatalogArtifact(
            artifact_id=str(row[0]),
            revision_id=str(row[1]),
            relative_path=str(row[2]),
            content_sha256=str(row[3]),
            byte_size=int(row[4]),
            mtime_ns=int(row[5]),
            artifact_type=str(row[6]),
            authority_class=str(row[7]),
            lifecycle_hints=tuple(json.loads(row[8])),
            source_scope=str(row[9]),
            repository=None if row[10] is None else str(row[10]),
            project=str(row[11]),
        )
        for row in rows
    ]
    return run_id, artifacts


def _read_verified_source(path: Path, artifact: CatalogArtifact) -> str:
    """Read one stable regular-file descriptor and verify its catalog hash."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    content = bytearray()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SourceChangedError(f"source is not a regular file: {path}")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            content.extend(block)
        after = os.fstat(handle.fileno())
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity:
        raise SourceChangedError(f"source changed while being read: {path}")
    actual = digest.hexdigest()
    if actual != artifact.content_sha256:
        raise SourceChangedError(
            f"source hash differs from catalog revision: {artifact.relative_path}"
        )
    return bytes(content).decode("utf-8", errors="replace")


def _split_oversized(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        boundary = remaining.rfind("\n", 0, max_chars + 1)
        if boundary < max_chars // 2:
            boundary = remaining.rfind(" ", 0, max_chars + 1)
        if boundary < max_chars // 2:
            boundary = max_chars
        piece = remaining[:boundary].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[boundary:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _blocks(text: str, max_chars: int) -> list[str]:
    """Return paragraph, heading, and fenced-code blocks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    current: list[str] = []
    fenced: list[str] = []
    fence_marker: str | None = None

    def flush_current() -> None:
        value = "\n".join(current).strip()
        current.clear()
        if value:
            blocks.extend(_split_oversized(value, max_chars))

    for line in text.split("\n"):
        fence_match = FENCE.match(line)
        if fence_marker is not None:
            fenced.append(line)
            if fence_match and line.lstrip().startswith(fence_marker):
                value = "\n".join(fenced).strip()
                fenced.clear()
                fence_marker = None
                blocks.extend(_split_oversized(value, max_chars))
            continue
        if fence_match:
            flush_current()
            fence_marker = fence_match.group(1)[0:3]
            fenced.append(line)
            continue
        if HEADING.match(line):
            flush_current()
            blocks.append(line.strip())
            continue
        if not line.strip():
            flush_current()
            continue
        current.append(line.rstrip())

    flush_current()
    if fenced:
        blocks.extend(_split_oversized("\n".join(fenced).strip(), max_chars))
    return blocks


def _overlap_tail(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or not text:
        return ""
    tail = text[-overlap_chars:]
    first_break = tail.find("\n")
    if 0 <= first_break < len(tail) // 2:
        tail = tail[first_break + 1 :]
    else:
        first_space = tail.find(" ")
        if 0 <= first_space < len(tail) // 2:
            tail = tail[first_space + 1 :]
    return tail.strip()


def split_text(
    text: str,
    *,
    max_chars: int = 5000,
    overlap_chars: int = 400,
) -> list[TextChunk]:
    if max_chars < 256:
        raise IngestionError("max_chars must be at least 256")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise IngestionError("overlap_chars must be >= 0 and smaller than max_chars")
    blocks = _blocks(text, max_chars)
    if not blocks:
        return []

    core_chunks: list[tuple[str, str | None]] = []
    current: list[str] = []
    heading_stack: list[str] = []
    current_heading: str | None = None

    def flush() -> None:
        nonlocal current_heading
        value = "\n\n".join(current).strip()
        current.clear()
        if value:
            core_chunks.append((value, current_heading))

    for block in blocks:
        match = HEADING.match(block)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_stack[level - 1 :] = [title]
            current_heading = " > ".join(heading_stack)
        candidate = block if not current else "\n\n".join(current + [block])
        if current and len(candidate) > max_chars:
            flush()
        current.append(block)
    flush()

    chunks: list[TextChunk] = []
    previous = ""
    for index, (core, heading) in enumerate(core_chunks):
        overlap = _overlap_tail(previous, overlap_chars)
        embedding_text = f"{overlap}\n\n{core}" if overlap else core
        chunks.append(
            TextChunk(
                index=index,
                content=core,
                embedding_text=embedding_text,
                overlap_prefix_chars=len(overlap),
                heading=heading,
            )
        )
        previous = core
    return chunks


def _reference_time(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc).isoformat()


def _graphiti_namespace(artifact: CatalogArtifact, group_prefix: str) -> str:
    scope = artifact.repository or f"{artifact.source_scope}:{artifact.project}"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", scope).strip("_").lower()
    if not slug:
        slug = "workspace"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:10]
    return f"{group_prefix}_{slug[:48]}_{digest}"


def build_unit(
    artifact: CatalogArtifact,
    chunk: TextChunk,
    catalog_run_id: int,
    group_prefix: str,
) -> dict[str, Any]:
    content_sha = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
    identity = f"{artifact.revision_id}\0{chunk.index}\0{content_sha}"
    unit_uuid = uuid.uuid5(UNIT_NAMESPACE, identity)
    point_uuid = uuid.uuid5(POINT_NAMESPACE, identity)
    episode_key = uuid.uuid5(EPISODE_KEY_NAMESPACE, identity)
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "unit_id": str(unit_uuid),
        "qdrant_point_id": str(point_uuid),
        "graphiti_episode_key": str(episode_key),
        "catalog_run_id": catalog_run_id,
        "artifact_id": artifact.artifact_id,
        "revision_id": artifact.revision_id,
        "relative_path": artifact.relative_path,
        "content_sha256": artifact.content_sha256,
        "artifact_byte_size": artifact.byte_size,
        "artifact_mtime_ns": artifact.mtime_ns,
        "reference_time": _reference_time(artifact.mtime_ns),
        "artifact_type": artifact.artifact_type,
        "authority_class": artifact.authority_class,
        "lifecycle_hints": list(artifact.lifecycle_hints),
        "source_scope": artifact.source_scope,
        "repository": artifact.repository,
        "project": artifact.project,
        "chunk_index": chunk.index,
        "chunk_sha256": content_sha,
        "heading": chunk.heading,
        "overlap_prefix_chars": chunk.overlap_prefix_chars,
        "content": chunk.content,
        "embedding_text": chunk.embedding_text,
        "graphiti_group_id": _graphiti_namespace(artifact, group_prefix),
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    security.atomic_write_json(path, payload)


@contextmanager
def _outbox_publish_lock(output_root: Path) -> Iterator[None]:
    """Serialize final-directory publication without exposing partial names."""
    security.ensure_private_directory(output_root)
    lock_path = output_root / ".outbox-publish.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, security.PRIVATE_FILE_MODE)
    try:
        security.secure_created_file(lock_path)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _new_outbox_temporary(output_root: Path, final_name: str) -> Path:
    security.ensure_private_directory(output_root)
    temporary = output_root / (
        f".tmp-{final_name}-{os.getpid()}-{uuid.uuid4().hex}"
    )
    security.ensure_private_directory(temporary)
    return temporary


def _publish_outbox_directory(
    temporary: Path,
    final: Path,
    *,
    fault: security.FaultHook | None = None,
) -> None:
    """Atomically make a fully durable outbox directory visible."""
    security.fsync_directory(temporary)
    if fault is not None:
        fault("before_publish")
    with _outbox_publish_lock(final.parent):
        if final.exists():
            raise FileExistsError(final)
        os.rename(temporary, final)
        security.require_private_directory(final)
        security.fsync_directory(final.parent)
    if fault is not None:
        fault("after_publish")


def prepare_outbox(
    *,
    workspace: Path,
    catalog: Path,
    output_root: Path,
    max_chars: int,
    overlap_chars: int,
    group_id: str,
    limit_artifacts: int = 0,
    _fault: security.FaultHook | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    catalog = _outside_workspace(catalog, workspace, "catalog")
    output_root = _outside_workspace(output_root, workspace, "outbox root")
    if not GROUP_ID.fullmatch(group_id):
        raise IngestionError("Graphiti group_id must contain only letters, digits, _ or -")

    run_id, artifacts = load_current_artifacts(catalog)
    if limit_artifacts > 0:
        artifacts = artifacts[:limit_artifacts]
    outbox = output_root / f"catalog-run-{run_id}-chunks-v{OUTBOX_SCHEMA_VERSION}"
    security.ensure_private_directory(output_root)
    if outbox.exists():
        existing = load_outbox_manifest(outbox)
        if (
            int(existing.get("catalog_run_id", -1)) != run_id
            or existing.get("graphiti_group_prefix") != group_id
            or existing.get("chunking")
            != {
                "max_chars": max_chars,
                "overlap_chars": overlap_chars,
                "version": 1,
            }
            or int(existing.get("catalog_artifacts_considered", -1))
            != len(artifacts)
        ):
            raise IngestionError(
                f"existing outbox identity differs; refusing overwrite: {outbox}"
            )
        # Iteration verifies the compressed bytes against the manifest.
        sum(1 for _ in iter_outbox_units(outbox))
        return {"outbox": str(outbox), "publication": "idempotent", **existing}

    temporary = _new_outbox_temporary(output_root, outbox.name)
    units_path = temporary / "ingest-units.jsonl.gz"
    manifest_path = temporary / "manifest.json"
    units_digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    started = datetime.now(timezone.utc).isoformat()

    try:
        with units_path.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for artifact in artifacts:
                    source = workspace / artifact.relative_path
                    try:
                        source.relative_to(workspace)
                    except ValueError as exc:
                        raise IngestionError(
                            f"catalog source escapes workspace: {artifact.relative_path}"
                        ) from exc
                    resolved_source = source.resolve(strict=True)
                    if not _inside(resolved_source, workspace):
                        raise IngestionError(
                            f"resolved catalog source escapes workspace: "
                            f"{artifact.relative_path}"
                        )
                    text = _read_verified_source(resolved_source, artifact)
                    chunks = split_text(
                        text,
                        max_chars=max_chars,
                        overlap_chars=overlap_chars,
                    )
                    if not chunks:
                        counts["empty_artifacts"] += 1
                        continue
                    counts["artifacts"] += 1
                    counts["source_bytes"] += artifact.byte_size
                    for chunk in chunks:
                        unit = build_unit(artifact, chunk, run_id, group_id)
                        line = (
                            json.dumps(
                                unit,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            )
                            + "\n"
                        ).encode("utf-8")
                        units_digest.update(line)
                        compressed.write(line)
                        counts["units"] += 1
                        counts["content_chars"] += len(chunk.content)
                        counts["embedding_chars"] += len(chunk.embedding_text)
            raw.flush()
            os.fsync(raw.fileno())
        security.secure_created_file(units_path)
        if _fault is not None:
            _fault("after_units_fsync")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "outbox_schema_version": OUTBOX_SCHEMA_VERSION,
            "complete": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started,
            "source_mutation": "disabled",
            "sink_deletion": "disabled",
            "workspace": str(workspace),
            "catalog": str(catalog),
            "catalog_run_id": run_id,
            "graphiti_group_prefix": group_id,
            "chunking": {
                "max_chars": max_chars,
                "overlap_chars": overlap_chars,
                "version": 1,
            },
            "counts": dict(sorted(counts.items())),
            "catalog_artifacts_considered": len(artifacts),
            "units_file": units_path.name,
            "units_sha256": units_digest.hexdigest(),
        }
        _write_manifest(manifest_path, manifest)
        if _fault is not None:
            _fault("after_manifest_fsync")
        try:
            _publish_outbox_directory(temporary, outbox, fault=_fault)
        except FileExistsError:
            existing = load_outbox_manifest(outbox)
            comparable = {
                key: existing.get(key)
                for key in manifest
                if key not in {"created_at", "started_at"}
            }
            expected = {
                key: value
                for key, value in manifest.items()
                if key not in {"created_at", "started_at"}
            }
            if comparable != expected:
                raise IngestionError(
                    f"concurrent outbox identity differs; refusing overwrite: {outbox}"
                )
            return {
                "outbox": str(outbox),
                "publication": "idempotent",
                **existing,
            }
        return {"outbox": str(outbox), "publication": "created", **manifest}
    except BaseException:
        # A crash may leave only a hidden private temporary directory. It is
        # never a published outbox and cannot block a later retry.
        raise


def load_outbox_manifest(outbox: Path) -> dict[str, Any]:
    outbox = outbox.expanduser().resolve(strict=True)
    security.require_private_directory(outbox)
    manifest_path = outbox / "manifest.json"
    if not manifest_path.is_file():
        raise IngestionError(f"outbox is incomplete; manifest missing: {outbox}")
    security.require_private_file(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("complete") is not True:
        raise IngestionError(f"outbox is not marked complete: {outbox}")
    if (
        int(manifest.get("outbox_schema_version", -1))
        not in SUPPORTED_OUTBOX_SCHEMA_VERSIONS
    ):
        raise IngestionError("unsupported outbox schema")
    units = outbox / str(manifest["units_file"])
    if not units.is_file():
        raise IngestionError(f"outbox units file is missing: {units}")
    security.require_private_file(units)
    return manifest


def iter_outbox_units(outbox: Path) -> Iterator[dict[str, Any]]:
    manifest = load_outbox_manifest(outbox)
    expected_schema = int(manifest["outbox_schema_version"])
    units = outbox.expanduser().resolve() / str(manifest["units_file"])
    digest = hashlib.sha256()
    with gzip.open(units, "rb") as handle:
        for raw in handle:
            digest.update(raw)
            unit = json.loads(raw)
            if int(unit.get("schema_version", -1)) != expected_schema:
                raise IngestionError("unit has unsupported schema")
            yield unit
    if digest.hexdigest() != manifest["units_sha256"]:
        raise IngestionError("outbox units checksum does not match manifest")


class IngestionState:
    def __init__(self, path: Path, workspace: Path):
        security.activate_private_umask()
        self.path = _outside_workspace(path, workspace, "ingestion state")
        security.ensure_private_directory(self.path.parent)
        if self.path.exists():
            security.require_private_file(self.path)
        self.connection = sqlite3.connect(self.path)
        security.secure_created_file(self.path)
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, STATE_SCHEMA_VERSION):
            self.connection.close()
            raise IngestionError(
                f"unsupported ingestion-state schema {version}; "
                f"expected {STATE_SCHEMA_VERSION}"
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sink_units (
              sink TEXT NOT NULL,
              target TEXT NOT NULL,
              unit_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              external_id TEXT,
              detail TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (sink, target, unit_id)
            );
            CREATE INDEX IF NOT EXISTS sink_units_status_idx
              ON sink_units(sink, target, status);
            """
        )
        self.connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            if candidate.exists():
                security.secure_created_file(candidate)

    def statuses(self, sink: str, target: str) -> dict[str, str]:
        return {
            str(row[0]): str(row[1])
            for row in self.connection.execute(
                "SELECT unit_id, status FROM sink_units WHERE sink=? AND target=?",
                (sink, target),
            )
        }

    def set_status(
        self,
        *,
        sink: str,
        target: str,
        unit_id: str,
        revision_id: str,
        status: str,
        external_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sink_units(
                  sink, target, unit_id, revision_id, status, attempt_count,
                  external_id, detail, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(sink, target, unit_id) DO UPDATE SET
                  revision_id=excluded.revision_id,
                  status=excluded.status,
                  attempt_count=sink_units.attempt_count + 1,
                  external_id=excluded.external_id,
                  detail=excluded.detail,
                  updated_at=excluded.updated_at
                """,
                (
                    sink,
                    target,
                    unit_id,
                    revision_id,
                    status,
                    external_id,
                    detail,
                    now,
                ),
            )

    def summary(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT sink, target, status, COUNT(*)
            FROM sink_units
            GROUP BY sink, target, status
            ORDER BY sink, target, status
            """
        ).fetchall()
        return [
            {
                "sink": str(row[0]),
                "target": str(row[1]),
                "status": str(row[2]),
                "count": int(row[3]),
            }
            for row in rows
        ]


def _current_revision_ids(catalog: Path, workspace: Path) -> set[str]:
    _run_id, artifacts = load_current_artifacts(
        _outside_workspace(catalog, workspace, "catalog")
    )
    return {artifact.revision_id for artifact in artifacts}


def _unit_payload(
    unit: dict[str, Any],
    embedding_model: str,
    *,
    catalog_current: bool,
    collection_generation: str = DEFAULT_COLLECTION_GENERATION,
    embedding_model_digest: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": int(unit["schema_version"]),
        "unit_id": unit["unit_id"],
        "artifact_id": unit["artifact_id"],
        "revision_id": unit["revision_id"],
        "relative_path": unit["relative_path"],
        "content_sha256": unit["content_sha256"],
        "artifact_type": unit["artifact_type"],
        "authority_class": unit["authority_class"],
        "lifecycle_hints": unit["lifecycle_hints"],
        "source_scope": unit["source_scope"],
        "repository": unit["repository"],
        "project": unit["project"],
        "chunk_index": unit["chunk_index"],
        "chunk_sha256": unit["chunk_sha256"],
        "heading": unit["heading"],
        "reference_time": unit["reference_time"],
        "catalog_run_id": unit["catalog_run_id"],
        "catalog_current": catalog_current,
        "embedding_model": embedding_model,
        "chunk_profile_id": DEFAULT_CHUNK_PROFILE_ID,
        "normalization_version": DEFAULT_NORMALIZATION_VERSION,
        "collection_generation": collection_generation,
        "content": unit["content"],
    }
    if embedding_model_digest:
        payload["embedding_model_digest"] = embedding_model_digest
    return payload


def _ensure_qdrant_payload_indexes(
    client: Any,
    models: Any,
    collection: str,
) -> list[str]:
    information = client.get_collection(collection)
    existing = set((information.payload_schema or {}).keys())
    created: list[str] = []
    schemas = {
        "keyword": models.PayloadSchemaType.KEYWORD,
        "bool": models.PayloadSchemaType.BOOL,
    }
    for field_name, schema_name in QDRANT_PAYLOAD_INDEXES.items():
        if field_name in existing:
            continue
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=schemas[schema_name],
            wait=True,
        )
        created.append(field_name)
    return created


def _qdrant_target(
    location: str,
    collection: str,
    model: str,
    *,
    collection_generation: str = DEFAULT_COLLECTION_GENERATION,
    embedding_model_digest: str | None = None,
) -> str:
    base = f"{location}|{collection}|{model}"
    if (
        collection_generation == DEFAULT_COLLECTION_GENERATION
        and embedding_model_digest is None
    ):
        # Preserve the original embedded target identity exactly.  Every new
        # server generation uses the extended identity below.
        return base
    return (
        f"{base}|generation:{collection_generation}|"
        f"model-digest:{embedding_model_digest or 'unrecorded'}|"
        f"chunk-profile:{DEFAULT_CHUNK_PROFILE_ID}|"
        f"normalization:{DEFAULT_NORMALIZATION_VERSION}"
    )


def require_loopback_qdrant_url(url: str) -> str:
    """Accept only an uncredentialed HTTP URL on the IPv4 loopback boundary."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise IngestionError(
            "Qdrant server URL must be an uncredentialed http://127.0.0.1:<port>"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise IngestionError("Qdrant server URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise IngestionError("Qdrant server URL must include a valid port")
    return f"http://127.0.0.1:{port}"


def _qdrant_api_key(explicit: str | None, environment_name: str) -> str:
    value = explicit if explicit is not None else os.environ.get(environment_name)
    if not value or not value.strip():
        raise IngestionError(
            f"Qdrant API key is required via {environment_name} or an explicit key"
        )
    return value.strip()


def create_text_embedder(embedding_model: str) -> Any:
    """Create one model instance in the private, stable artifact cache."""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise IngestionError("FastEmbed dependency is missing") from exc
    security.ensure_private_directory(DEFAULT_MODEL_CACHE)
    return TextEmbedding(
        model_name=embedding_model,
        cache_dir=str(DEFAULT_MODEL_CACHE),
    )


def qdrant_ingest(
    *,
    workspace: Path,
    catalog: Path,
    outbox: Path,
    state_path: Path,
    collection: str,
    embedding_model: str,
    local_path: Path | None,
    url: str | None,
    api_key_env: str,
    batch_size: int,
    limit_units: int,
    apply: bool,
    api_key: str | None = None,
    client: Any | None = None,
    embedder: Any | None = None,
    collection_generation: str = DEFAULT_COLLECTION_GENERATION,
    embedding_model_digest: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    load_outbox_manifest(outbox)
    current_revisions = _current_revision_ids(catalog, workspace)
    if bool(local_path) == bool(url):
        raise IngestionError("choose exactly one of --qdrant-path or --qdrant-url")
    if batch_size < 1:
        raise IngestionError("batch_size must be positive")
    location: str
    if local_path:
        resolved_local = _outside_workspace(local_path, workspace, "Qdrant path")
        if resolved_local.exists():
            security.require_private_directory(resolved_local)
        location = f"local:{resolved_local}"
    else:
        url = require_loopback_qdrant_url(str(url))
        location = f"url:{url}"
    target = _qdrant_target(
        location,
        collection,
        embedding_model,
        collection_generation=collection_generation,
        embedding_model_digest=embedding_model_digest,
    )
    state = IngestionState(state_path, workspace)
    statuses = state.statuses("qdrant", target)
    selected: list[dict[str, Any]] = []
    total = 0
    completed = 0
    for unit in iter_outbox_units(outbox):
        total += 1
        if limit_units > 0 and total > limit_units:
            continue
        if statuses.get(str(unit["unit_id"])) == "completed":
            completed += 1
            continue
        selected.append(unit)
    if not apply:
        result = {
            "mode": "plan",
            "sink": "qdrant",
            "target": target,
            "outbox_units": total,
            "already_completed": completed,
            "would_ingest": len(selected),
        }
        state.close()
        return result

    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        state.close()
        raise IngestionError(
            "Qdrant dependencies are missing; install requirements-artifact-ingestion.txt"
        ) from exc

    owns_client = client is None
    if client is None:
        if local_path:
            security.ensure_private_directory(resolved_local)
            client = QdrantClient(path=str(resolved_local))
        else:
            client = QdrantClient(
                url=url,
                api_key=_qdrant_api_key(api_key, api_key_env),
                timeout=30,
            )
    if embedder is None:
        embedder = create_text_embedder(embedding_model)
    created_collection = False
    created_indexes: list[str] = []
    indexes_ensured = False
    ingested = 0
    failures = 0
    try:
        for offset in range(0, len(selected), batch_size):
            batch = selected[offset : offset + batch_size]
            vectors = list(
                embedder.embed([str(unit["embedding_text"]) for unit in batch])
            )
            # Callers treat a non-raising return from this function as proof
            # that every SELECTED unit landed — the consumer clears its durable
            # publication-failure row on exactly that basis. Two embedder
            # shortfalls used to break that silently: an empty yield skipped the
            # whole batch (`continue`), and a short yield was truncated by the
            # zip below — yet BOTH still marked the entire batch 'completed' and
            # added len(batch) to `ingested`, writing checkpoint rows for units
            # that were never upserted. That strands content behind a success
            # report (and hides it from the catalog-vs-checkpoint anti-join).
            # Fail loudly instead; the caller's retry is idempotent.
            if len(vectors) != len(batch):
                raise IngestionError(
                    f"embedder returned {len(vectors)} vectors for a batch of "
                    f"{len(batch)} units; refusing to report partial success"
                )
            dimension = len(vectors[0])
            if not client.collection_exists(collection):
                client.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(
                        size=dimension,
                        distance=models.Distance.COSINE,
                    ),
                )
                created_collection = True
            else:
                information = client.get_collection(collection)
                vectors_config = information.config.params.vectors
                existing_dimension = getattr(vectors_config, "size", None)
                if (
                    existing_dimension is not None
                    and int(existing_dimension) != dimension
                ):
                    raise IngestionError(
                        f"Qdrant collection dimension {existing_dimension} "
                        f"does not match model dimension {dimension}"
                    )
                sample, _next_page = client.scroll(
                    collection_name=collection,
                    limit=1,
                    with_payload=["embedding_model"],
                    with_vectors=False,
                )
                if sample:
                    existing_model = (sample[0].payload or {}).get("embedding_model")
                    if existing_model and existing_model != embedding_model:
                        raise IngestionError(
                            f"Qdrant collection uses embedding model "
                            f"{existing_model!r}, not {embedding_model!r}"
                        )
            if not indexes_ensured:
                created_indexes = _ensure_qdrant_payload_indexes(
                    client,
                    models,
                    collection,
                )
                indexes_ensured = True
            points = [
                models.PointStruct(
                    id=str(unit["qdrant_point_id"]),
                    vector=vector.tolist(),
                    payload=_unit_payload(
                        unit,
                        embedding_model,
                        catalog_current=str(unit["revision_id"])
                        in current_revisions,
                        collection_generation=collection_generation,
                        embedding_model_digest=embedding_model_digest,
                    ),
                )
                for unit, vector in zip(batch, vectors)
            ]
            try:
                client.upsert(
                    collection_name=collection,
                    points=points,
                    wait=True,
                )
            except BaseException as exc:
                failures += len(batch)
                for unit in batch:
                    state.set_status(
                        sink="qdrant",
                        target=target,
                        unit_id=str(unit["unit_id"]),
                        revision_id=str(unit["revision_id"]),
                        status="failed",
                        detail=f"{type(exc).__name__}: {exc}"[:1000],
                    )
                raise
            for unit in batch:
                state.set_status(
                    sink="qdrant",
                    target=target,
                    unit_id=str(unit["unit_id"]),
                    revision_id=str(unit["revision_id"]),
                    status="completed",
                    external_id=str(unit["qdrant_point_id"]),
                )
            ingested += len(batch)
        count = client.count(collection_name=collection, exact=True).count
        return {
            "mode": "applied",
            "sink": "qdrant",
            "target": target,
            "created_collection": created_collection,
            "created_payload_indexes": created_indexes,
            "ingested": ingested,
            "failed": failures,
            "collection_points": int(count),
        }
    finally:
        if owns_client:
            client.close()
        state.close()


def qdrant_reconcile(
    *,
    workspace: Path,
    catalog: Path,
    collection: str,
    local_path: Path | None,
    url: str | None,
    api_key_env: str,
    batch_size: int,
    apply: bool,
    api_key: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Reconcile additive lifecycle payloads without deleting any point."""
    workspace = workspace.expanduser().resolve(strict=True)
    current_revisions = _current_revision_ids(catalog, workspace)
    if bool(local_path) == bool(url):
        raise IngestionError("choose exactly one Qdrant location")
    if batch_size < 1:
        raise IngestionError("batch_size must be positive")
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise IngestionError("Qdrant dependencies are missing") from exc
    owns_client = client is None
    if local_path:
        resolved = _outside_workspace(local_path, workspace, "Qdrant path")
        security.require_private_directory(resolved)
        if client is None:
            client = QdrantClient(path=str(resolved))
        location = f"local:{resolved}"
    else:
        url = require_loopback_qdrant_url(str(url))
        if client is None:
            client = QdrantClient(
                url=url,
                api_key=_qdrant_api_key(api_key, api_key_env),
                timeout=30,
            )
        location = f"url:{url}"

    scanned = 0
    expected_current = 0
    expected_historical = 0
    to_current: list[Any] = []
    to_historical: list[Any] = []
    malformed: list[str] = []
    offset: Any = None
    try:
        if not client.collection_exists(collection):
            raise IngestionError(f"Qdrant collection does not exist: {collection}")
        while True:
            points, next_offset = client.scroll(
                collection_name=collection,
                limit=512,
                offset=offset,
                with_payload=["revision_id", "catalog_current"],
                with_vectors=False,
            )
            for point in points:
                scanned += 1
                payload = dict(point.payload or {})
                revision_id = payload.get("revision_id")
                if not isinstance(revision_id, str) or not revision_id:
                    malformed.append(str(point.id))
                    continue
                should_be_current = revision_id in current_revisions
                if should_be_current:
                    expected_current += 1
                else:
                    expected_historical += 1
                if payload.get("catalog_current") is should_be_current:
                    continue
                if should_be_current:
                    to_current.append(point.id)
                else:
                    to_historical.append(point.id)
            if next_offset is None:
                break
            offset = next_offset
        if malformed:
            raise IngestionError(
                "Qdrant points are missing revision_id; refusing partial reconcile: "
                + ", ".join(malformed[:5])
            )
        result = {
            "mode": "applied" if apply else "plan",
            "sink": "qdrant",
            "target": f"{location}|{collection}",
            "catalog_current_revision_count": len(current_revisions),
            "points_scanned": scanned,
            "points_current": expected_current,
            "points_historical": expected_historical,
            "would_mark_current": len(to_current),
            "would_mark_historical": len(to_historical),
            "created_payload_indexes": [],
            "source_mutation": "disabled",
            "point_deletion": "disabled",
        }
        if not apply:
            return result
        result["created_payload_indexes"] = _ensure_qdrant_payload_indexes(
            client,
            models,
            collection,
        )
        for point_ids, value in ((to_current, True), (to_historical, False)):
            for start in range(0, len(point_ids), batch_size):
                client.set_payload(
                    collection_name=collection,
                    payload={"catalog_current": value},
                    points=point_ids[start : start + batch_size],
                    wait=True,
                )
        result["marked_current"] = len(to_current)
        result["marked_historical"] = len(to_historical)
        return result
    finally:
        if owns_client:
            client.close()


def _qdrant_query_filter(
    models: Any,
    *,
    current_only: bool,
    project: str | None,
    artifact_type: str | None,
    authority_class: str | None,
    repository: str | None,
    lifecycle_hint: str | None,
) -> Any:
    values = {
        "project": project,
        "artifact_type": artifact_type,
        "authority_class": authority_class,
        "repository": repository,
        "lifecycle_hints": lifecycle_hint,
    }
    conditions = [
        models.FieldCondition(
            key=field_name,
            match=models.MatchValue(value=value),
        )
        for field_name, value in values.items()
        if value is not None
    ]
    if current_only:
        conditions.append(
            models.FieldCondition(
                key="catalog_current",
                match=models.MatchValue(value=True),
            )
        )
    return models.Filter(must=conditions) if conditions else None


def qdrant_search(
    *,
    workspace: Path,
    catalog: Path,
    local_path: Path | None,
    url: str | None,
    api_key_env: str,
    collection: str,
    embedding_model: str,
    query: str,
    limit: int,
    current_only: bool,
    project: str | None = None,
    artifact_type: str | None = None,
    authority_class: str | None = None,
    repository: str | None = None,
    lifecycle_hint: str | None = None,
    api_key: str | None = None,
    client: Any | None = None,
    embedder: Any | None = None,
    query_vector: Sequence[float] | None = None,
    current_revisions_override: set[str] | None = None,
    verify_lifecycle_payload: bool = True,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise IngestionError("Qdrant dependencies are missing") from exc
    if bool(local_path) == bool(url):
        raise IngestionError("choose exactly one Qdrant location")
    owns_client = client is None
    if local_path:
        resolved = _outside_workspace(local_path, workspace, "Qdrant path")
        if client is None:
            client = QdrantClient(path=str(resolved))
    else:
        url = require_loopback_qdrant_url(str(url))
        if client is None:
            client = QdrantClient(
                url=url,
                api_key=_qdrant_api_key(api_key, api_key_env),
                timeout=10,
            )
    current_revisions: set[str] | None = None
    if current_only:
        if current_revisions_override is not None:
            current_revisions = current_revisions_override
        else:
            _run, artifacts = load_current_artifacts(
                _outside_workspace(catalog, workspace, "catalog")
            )
            current_revisions = {artifact.revision_id for artifact in artifacts}
    if query_vector is None:
        if embedder is None:
            embedder = create_text_embedder(embedding_model)
        vector = list(embedder.embed([query]))[0].tolist()
    else:
        vector = list(query_vector)
    try:
        if not client.collection_exists(collection):
            raise IngestionError(f"Qdrant collection does not exist: {collection}")
        if current_only and verify_lifecycle_payload:
            sample, _sample_offset = client.scroll(
                collection_name=collection,
                limit=1,
                with_payload=["catalog_current"],
                with_vectors=False,
            )
            if sample and "catalog_current" not in (sample[0].payload or {}):
                raise IngestionError(
                    "Qdrant lifecycle payloads are missing; run qdrant-reconcile"
                )
        query_filter = _qdrant_query_filter(
            models,
            current_only=current_only,
            project=project,
            artifact_type=artifact_type,
            authority_class=authority_class,
            repository=repository,
            lifecycle_hint=lifecycle_hint,
        )
        response = client.query_points(
            collection_name=collection,
            query=vector,
            query_filter=query_filter,
            limit=max(limit * 5, limit),
            with_payload=True,
        )
        results = []
        for point in response.points:
            payload = dict(point.payload or {})
            if (
                current_revisions is not None
                and payload.get("revision_id") not in current_revisions
            ):
                continue
            results.append(
                {
                    "score": float(point.score),
                    "point_id": str(point.id),
                    "relative_path": payload.get("relative_path"),
                    "heading": payload.get("heading"),
                    "artifact_type": payload.get("artifact_type"),
                    "revision_id": payload.get("revision_id"),
                    "content": payload.get("content"),
                }
            )
            if len(results) >= limit:
                break
        return {
            "query": query,
            "current_only": current_only,
            "filters": {
                key: value
                for key, value in {
                    "project": project,
                    "artifact_type": artifact_type,
                    "authority_class": authority_class,
                    "repository": repository,
                    "lifecycle_hint": lifecycle_hint,
                }.items()
                if value is not None
            },
            "results": results,
        }
    finally:
        if owns_client:
            client.close()


def _graphiti_episode_body(unit: dict[str, Any]) -> str:
    metadata = [
        f"Artifact path: {unit['relative_path']}",
        f"Artifact type: {unit['artifact_type']}",
        f"Project: {unit['project']}",
        f"Revision: {unit['revision_id']}",
        f"Chunk: {unit['chunk_index']}",
    ]
    if unit.get("heading"):
        metadata.append(f"Section: {unit['heading']}")
    return "\n".join(metadata) + "\n\n" + str(unit["content"])


def _require_falkordb_endpoint(host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        raise IngestionError(
            f"FalkorDB is not reachable at {host}:{port}: {exc}"
        ) from exc


def _validate_graphiti_result_namespace(
    result: Any,
    expected_group_id: str,
) -> dict[str, int]:
    """Fail closed if Graphiti returns data outside the requested namespace."""
    collections = {
        "episode": (result.episode,),
        "nodes": tuple(result.nodes),
        "edges": tuple(result.edges),
        "episodic_edges": tuple(result.episodic_edges),
    }
    for label, values in collections.items():
        for value in values:
            actual = getattr(value, "group_id", None)
            if actual != expected_group_id:
                raise IngestionError(
                    f"Graphiti {label} escaped namespace: "
                    f"expected {expected_group_id!r}, got {actual!r}"
                )
    return {
        "nodes": len(collections["nodes"]),
        "edges": len(collections["edges"]),
        "episodic_edges": len(collections["episodic_edges"]),
    }


def graphiti_readiness(
    *,
    host: str,
    port: int,
    password_env: str,
    llm_base_url: str,
    llm_model: str | None,
    embedding_base_url: str,
    embedding_model: str | None,
    embedding_dim: int,
    api_key_env: str,
    probe_models: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Probe required services without creating a graph or ingesting an episode."""
    gates: dict[str, dict[str, Any]] = {}
    password = os.environ.get(password_env)
    database_client: Any = None
    try:
        from falkordb import FalkorDB

        database_client = FalkorDB(
            host=host,
            port=port,
            password=password,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
        )
        graphs = database_client.list_graphs()
        gates["falkordb"] = {
            "status": "pass",
            "endpoint": f"{host}:{port}",
            "graph_count": len(graphs),
        }
    except Exception as exc:
        gates["falkordb"] = {
            "status": "fail",
            "endpoint": f"{host}:{port}",
            "detail": f"{type(exc).__name__}: {exc}"[:1000],
        }
    finally:
        if database_client is not None:
            database_client.close()

    configuration_errors: list[str] = []
    if not llm_model:
        configuration_errors.append("LLM model is unset")
    if not embedding_model:
        configuration_errors.append("embedding model is unset")
    if embedding_dim < 1:
        configuration_errors.append("embedding dimension must be positive")
    if configuration_errors:
        gates["configuration"] = {
            "status": "fail",
            "detail": "; ".join(configuration_errors),
        }
    else:
        gates["configuration"] = {
            "status": "pass",
            "llm_model": llm_model,
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
        }

    if not probe_models:
        gates["structured_output"] = {
            "status": "not_probed",
            "detail": "pass --probe-models to invoke the configured LLM",
        }
        gates["embedding"] = {
            "status": "not_probed",
            "detail": "pass --probe-models to invoke the configured embedding model",
        }
    elif configuration_errors:
        gates["structured_output"] = {
            "status": "blocked",
            "detail": "model configuration is incomplete",
        }
        gates["embedding"] = {
            "status": "blocked",
            "detail": "model configuration is incomplete",
        }
    else:
        api_key = os.environ.get(api_key_env)
        if not api_key and any(
            local in llm_base_url or local in embedding_base_url
            for local in ("127.0.0.1", "localhost")
        ):
            api_key = "ollama"
        if not api_key:
            gates["structured_output"] = {
                "status": "fail",
                "detail": f"API key environment variable is unset: {api_key_env}",
            }
            gates["embedding"] = {
                "status": "fail",
                "detail": f"API key environment variable is unset: {api_key_env}",
            }
        else:
            try:
                from openai import OpenAI
                from pydantic import BaseModel, Field

                class TemporalFactProbe(BaseModel):
                    subject: str
                    predicate: str
                    object: str
                    valid_at: str
                    confidence: float = Field(ge=0, le=1)

                llm_client = OpenAI(
                    base_url=llm_base_url,
                    api_key=api_key,
                    timeout=timeout_seconds,
                )
                response = llm_client.chat.completions.parse(
                    model=str(llm_model),
                    temperature=0,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Extract exactly one temporal fact. Return only "
                                "the requested schema."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "On 2026-07-17, the artifact pipeline "
                                "stored 250 chunks in Qdrant."
                            ),
                        },
                    ],
                    response_format=TemporalFactProbe,
                )
                fact = response.choices[0].message.parsed
                if fact is None or "workspace" not in fact.subject:
                    raise IngestionError(
                        "structured output parsed but did not preserve the subject"
                    )
                gates["structured_output"] = {
                    "status": "pass",
                    "model": llm_model,
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "valid_at": fact.valid_at,
                }
            except Exception as exc:
                gates["structured_output"] = {
                    "status": "fail",
                    "model": llm_model,
                    "detail": f"{type(exc).__name__}: {exc}"[:1000],
                }
            try:
                from openai import OpenAI

                embedding_client = OpenAI(
                    base_url=embedding_base_url,
                    api_key=api_key,
                    timeout=timeout_seconds,
                )
                response = embedding_client.embeddings.create(
                    model=str(embedding_model),
                    input="Personal temporal artifact readiness probe",
                    encoding_format="float",
                )
                vector = list(response.data[0].embedding)
                if len(vector) != embedding_dim:
                    raise IngestionError(
                        f"embedding dimension {len(vector)} does not match "
                        f"configured dimension {embedding_dim}"
                    )
                if not vector or not all(math.isfinite(value) for value in vector):
                    raise IngestionError("embedding contains no usable finite vector")
                norm = math.sqrt(math.fsum(value * value for value in vector))
                if norm <= 0:
                    raise IngestionError("embedding vector has zero norm")
                gates["embedding"] = {
                    "status": "pass",
                    "model": embedding_model,
                    "dimension": len(vector),
                    "norm": round(norm, 6),
                }
            except Exception as exc:
                gates["embedding"] = {
                    "status": "fail",
                    "model": embedding_model,
                    "detail": f"{type(exc).__name__}: {exc}"[:1000],
                }

    return {
        "mode": "read-only-readiness",
        "ready": all(gate.get("status") == "pass" for gate in gates.values()),
        "model_probes": "enabled" if probe_models else "disabled",
        "telemetry": "not-initialized",
        "graph_mutation": "disabled",
        "gates": gates,
    }


async def _graphiti_apply_async(
    *,
    units: list[dict[str, Any]],
    state: IngestionState,
    target: str,
    host: str,
    port: int,
    password_env: str,
    database: str,
    llm_base_url: str,
    llm_model: str,
    embedding_base_url: str,
    embedding_model: str,
    embedding_dim: int,
    api_key_env: str,
    structured_output_mode: str,
    instructions: str | None,
) -> dict[str, Any]:
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    os.environ.setdefault("SEMAPHORE_LIMIT", "1")
    try:
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import (
            OpenAIRerankerClient,
        )
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.openai import (
            OpenAIEmbedder,
            OpenAIEmbedderConfig,
        )
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import (
            OpenAIGenericClient,
        )
        from graphiti_core.nodes import EpisodeType
    except ImportError as exc:
        raise IngestionError(
            "Graphiti dependencies are missing; install requirements-artifact-ingestion.txt"
        ) from exc

    api_key = os.environ.get(api_key_env)
    if not api_key and (
        "127.0.0.1" in llm_base_url or "localhost" in llm_base_url
    ):
        api_key = "ollama"
    if not api_key:
        raise IngestionError(f"Graphiti LLM key environment variable is unset: {api_key_env}")
    password = os.environ.get(password_env)
    driver = FalkorDriver(
        host=host,
        port=port,
        password=password,
        database=database,
    )
    config = LLMConfig(
        api_key=api_key,
        model=llm_model,
        small_model=llm_model,
        base_url=llm_base_url,
    )
    llm = OpenAIGenericClient(
        config=config,
        structured_output_mode=structured_output_mode,
    )
    entity_types, edge_types, edge_type_map = graphiti_policy.graphiti_ontology()
    GuardedGraphiti = graphiti_policy.guarded_graphiti_class(Graphiti)
    graphiti = GuardedGraphiti(
        graph_driver=driver,
        llm_client=llm,
        embedder=OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=api_key,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                base_url=embedding_base_url,
            )
        ),
        cross_encoder=OpenAIRerankerClient(client=llm, config=config),
        max_coroutines=1,
    )
    completed = 0
    ambiguous = 0
    initialized_namespaces: set[str] = set()
    try:
        for unit in units:
            group_id = str(unit["graphiti_group_id"])
            if getattr(graphiti.driver, "_database", None) != group_id:
                # Graphiti's Falkor clone constructor schedules an untracked
                # background index task when called inside an event loop.
                # A shallow database switch lets this adapter await the one
                # schema initialization it owns.
                graphiti.driver = graphiti.driver.with_database(group_id)
                graphiti.clients.driver = graphiti.driver
            if group_id not in initialized_namespaces:
                await graphiti.build_indices_and_constraints()
                initialized_namespaces.add(group_id)
            state.set_status(
                sink="graphiti",
                target=target,
                unit_id=str(unit["unit_id"]),
                revision_id=str(unit["revision_id"]),
                status="in_progress",
                detail="write-ahead checkpoint; retry requires explicit review on failure",
            )
            try:
                with graphiti_policy.extracted_node_guard(
                    group_id=group_id,
                ):
                    result = await graphiti.add_episode(
                        name=f"{unit['relative_path']}#chunk-{unit['chunk_index']}",
                        episode_body=_graphiti_episode_body(unit),
                        source_description=(
                            "workspace artifact catalog revision "
                            f"{unit['revision_id']} ({unit['artifact_type']})"
                        ),
                        reference_time=datetime.fromisoformat(unit["reference_time"]),
                        source=EpisodeType.text,
                        group_id=group_id,
                        entity_types=entity_types,
                        excluded_entity_types=["Entity"],
                        edge_types=edge_types,
                        edge_type_map=edge_type_map,
                        custom_extraction_instructions=instructions,
                    )
                result_counts = _validate_graphiti_result_namespace(result, group_id)
            except BaseException as exc:
                ambiguous += 1
                state.set_status(
                    sink="graphiti",
                    target=target,
                    unit_id=str(unit["unit_id"]),
                    revision_id=str(unit["revision_id"]),
                    status="ambiguous",
                    detail=f"{type(exc).__name__}: {exc}"[:1000],
                )
                raise
            external_id = str(result.episode.uuid)
            state.set_status(
                sink="graphiti",
                target=target,
                unit_id=str(unit["unit_id"]),
                revision_id=str(unit["revision_id"]),
                status="completed",
                external_id=external_id,
                detail=(
                    f"episode_key={unit['graphiti_episode_key']}; "
                    f"namespace={group_id}; "
                    f"nodes={result_counts['nodes']}; "
                    f"edges={result_counts['edges']}; "
                    f"episodic_edges={result_counts['episodic_edges']}"
                ),
            )
            completed += 1
        return {
            "mode": "applied",
            "sink": "graphiti",
            "target": target,
            "completed": completed,
            "ambiguous": ambiguous,
        }
    finally:
        await graphiti.close()


def graphiti_ingest(
    *,
    workspace: Path,
    outbox: Path,
    state_path: Path,
    host: str,
    port: int,
    password_env: str,
    database: str,
    group_id: str,
    llm_base_url: str,
    llm_model: str | None,
    embedding_base_url: str,
    embedding_model: str | None,
    embedding_dim: int,
    api_key_env: str,
    structured_output_mode: str,
    instructions_file: Path | None,
    limit_units: int,
    retry_ambiguous: bool,
    apply: bool,
    artifact_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve(strict=True)
    manifest = load_outbox_manifest(outbox)
    if not GROUP_ID.fullmatch(group_id) or not GROUP_ID.fullmatch(database):
        raise IngestionError("Graphiti group and database names must be identifier-safe")
    manifest_group = manifest.get(
        "graphiti_group_prefix",
        manifest.get("graphiti_group_id"),
    )
    if manifest_group != group_id:
        raise IngestionError(
            "outbox Graphiti group prefix does not match requested group_id"
        )
    outbox_schema_version = int(manifest["outbox_schema_version"])
    if apply and outbox_schema_version < 2:
        raise IngestionError(
            "Graphiti apply requires a namespaced v2 outbox; "
            "v1 global-namespace ingestion is disabled"
        )
    target = (
        f"falkordb:{host}:{port}/{database}|group-prefix:{group_id}|"
        f"llm:{llm_base_url}:{llm_model or '(unset)'}|"
        f"embed:{embedding_base_url}:{embedding_model or '(unset)'}"
    )
    state = IngestionState(state_path, workspace)
    statuses = state.statuses("graphiti", target)
    selected_artifact_types = tuple(
        sorted(set(artifact_types or DEFAULT_GRAPHITI_ARTIFACT_TYPES))
    )
    if not selected_artifact_types:
        state.close()
        raise IngestionError("Graphiti requires at least one artifact type")
    selected: list[dict[str, Any]] = []
    total = 0
    eligible = 0
    filtered_out = 0
    namespaces: set[str] = set()
    completed = 0
    ambiguous = 0
    for unit in iter_outbox_units(outbox):
        total += 1
        if str(unit.get("artifact_type")) not in selected_artifact_types:
            filtered_out += 1
            continue
        eligible += 1
        namespaces.add(str(unit["graphiti_group_id"]))
        if limit_units > 0 and eligible > limit_units:
            continue
        status_value = statuses.get(str(unit["unit_id"]))
        if status_value == "completed":
            completed += 1
            continue
        if status_value in {"in_progress", "ambiguous"} and not retry_ambiguous:
            ambiguous += 1
            continue
        selected.append(unit)
    if not apply:
        result = {
            "mode": "plan",
            "sink": "graphiti",
            "backend": "falkordb",
            "target": target,
            "outbox_units": total,
            "eligible_units": eligible,
            "filtered_out_units": filtered_out,
            "artifact_types": list(selected_artifact_types),
            "namespace_count": len(namespaces),
            "outbox_schema_version": outbox_schema_version,
            "already_completed": completed,
            "ambiguous_requires_review": ambiguous,
            "would_ingest": len(selected),
            "telemetry": "disabled-on-apply",
        }
        state.close()
        return result
    if limit_units < 1 or limit_units > MAX_GRAPHITI_PILOT_UNITS:
        state.close()
        raise IngestionError(
            "Graphiti apply is quality-pilot-only and currently requires "
            f"--limit-units={MAX_GRAPHITI_PILOT_UNITS}"
        )
    if not llm_model or not embedding_model or embedding_dim < 1:
        state.close()
        raise IngestionError(
            "Graphiti apply requires LLM model, embedding model, and embedding dimension"
    )
    try:
        _require_falkordb_endpoint(host, port)
        instructions = DEFAULT_GRAPHITI_INSTRUCTIONS
        if instructions_file:
            instructions = instructions_file.expanduser().read_text(encoding="utf-8")
        result = asyncio.run(
            _graphiti_apply_async(
                units=selected,
                state=state,
                target=target,
                host=host,
                port=port,
                password_env=password_env,
                database=database,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                embedding_base_url=embedding_base_url,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                api_key_env=api_key_env,
                structured_output_mode=structured_output_mode,
                instructions=instructions,
            )
        )
        result.update(
            {
                "artifact_types": list(selected_artifact_types),
                "eligible_units": eligible,
                "filtered_out_units": filtered_out,
                "namespace_count": len(namespaces),
                "outbox_schema_version": outbox_schema_version,
            }
        )
        return result
    finally:
        state.close()


def _common_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[1]),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="build a verified compressed outbox")
    _common_workspace(prepare)
    prepare.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    prepare.add_argument(
        "--output-root",
        default=str(DEFAULT_DERIVED_ROOT / "outbox"),
    )
    prepare.add_argument("--max-chars", type=int, default=5000)
    prepare.add_argument("--overlap-chars", type=int, default=400)
    prepare.add_argument(
        "--graphiti-group-prefix",
        "--graphiti-group-id",
        dest="graphiti_group_prefix",
        default=DEFAULT_GRAPHITI_GROUP,
    )
    prepare.add_argument("--limit-artifacts", type=int, default=0)

    qdrant = commands.add_parser("qdrant", help="plan or apply Qdrant upserts")
    _common_workspace(qdrant)
    qdrant.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    qdrant.add_argument("--outbox", required=True)
    qdrant.add_argument("--state", default=str(DEFAULT_STATE))
    location = qdrant.add_mutually_exclusive_group()
    location.add_argument("--qdrant-path")
    location.add_argument("--qdrant-url")
    qdrant.add_argument("--api-key-env", default="QDRANT_API_KEY")
    qdrant.add_argument("--collection", default=DEFAULT_COLLECTION)
    qdrant.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    qdrant.add_argument("--batch-size", type=int, default=32)
    qdrant.add_argument("--limit-units", type=int, default=0)
    qdrant.add_argument("--apply", action="store_true")

    reconcile = commands.add_parser(
        "qdrant-reconcile",
        help="plan or apply additive Qdrant lifecycle payloads",
    )
    _common_workspace(reconcile)
    reconcile.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    reconcile_location = reconcile.add_mutually_exclusive_group()
    reconcile_location.add_argument("--qdrant-path")
    reconcile_location.add_argument("--qdrant-url")
    reconcile.add_argument("--api-key-env", default="QDRANT_API_KEY")
    reconcile.add_argument("--collection", default=DEFAULT_COLLECTION)
    reconcile.add_argument("--batch-size", type=int, default=256)
    reconcile.add_argument("--apply", action="store_true")

    search = commands.add_parser("search", help="query Qdrant and return current revisions")
    _common_workspace(search)
    search.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    search_location = search.add_mutually_exclusive_group()
    search_location.add_argument("--qdrant-path")
    search_location.add_argument("--qdrant-url")
    search.add_argument("--api-key-env", default="QDRANT_API_KEY")
    search.add_argument("--collection", default=DEFAULT_COLLECTION)
    search.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--project")
    search.add_argument("--artifact-type")
    search.add_argument("--authority-class")
    search.add_argument("--repository")
    search.add_argument("--lifecycle-hint")
    search.add_argument("--include-history", action="store_true")

    graphiti_check = commands.add_parser(
        "graphiti-check",
        help="probe FalkorDB and model readiness without graph mutation",
    )
    _common_workspace(graphiti_check)
    graphiti_check.add_argument("--host", default="127.0.0.1")
    graphiti_check.add_argument("--port", type=int, default=6379)
    graphiti_check.add_argument("--password-env", default="FALKORDB_PASSWORD")
    graphiti_check.add_argument(
        "--llm-base-url",
        default="http://127.0.0.1:11434/v1",
    )
    graphiti_check.add_argument("--llm-model")
    graphiti_check.add_argument(
        "--embedding-base-url",
        default="http://127.0.0.1:11434/v1",
    )
    graphiti_check.add_argument("--embedding-model")
    graphiti_check.add_argument("--embedding-dim", type=int, default=0)
    graphiti_check.add_argument("--api-key-env", default="GRAPHITI_LLM_API_KEY")
    graphiti_check.add_argument("--probe-models", action="store_true")
    graphiti_check.add_argument("--timeout-seconds", type=float, default=90)

    graphiti = commands.add_parser("graphiti", help="plan or apply FalkorDB Graphiti episodes")
    _common_workspace(graphiti)
    graphiti.add_argument("--outbox", required=True)
    graphiti.add_argument("--state", default=str(DEFAULT_STATE))
    graphiti.add_argument("--host", default="127.0.0.1")
    graphiti.add_argument("--port", type=int, default=6379)
    graphiti.add_argument("--password-env", default="FALKORDB_PASSWORD")
    graphiti.add_argument("--database", default=DEFAULT_GRAPHITI_GROUP)
    graphiti.add_argument(
        "--group-prefix",
        "--group-id",
        dest="group_prefix",
        default=DEFAULT_GRAPHITI_GROUP,
    )
    graphiti.add_argument("--llm-base-url", default="http://127.0.0.1:11434/v1")
    graphiti.add_argument("--llm-model")
    graphiti.add_argument(
        "--embedding-base-url",
        default="http://127.0.0.1:11434/v1",
    )
    graphiti.add_argument("--embedding-model")
    graphiti.add_argument("--embedding-dim", type=int, default=0)
    graphiti.add_argument("--api-key-env", default="GRAPHITI_LLM_API_KEY")
    graphiti.add_argument(
        "--structured-output-mode",
        choices=("json_schema", "json_object"),
        default="json_schema",
    )
    graphiti.add_argument("--instructions-file")
    graphiti.add_argument(
        "--artifact-type",
        action="append",
        choices=(
            "decision",
            "document",
            "handoff",
            "milestone_evidence",
            "plan",
            "research",
            "roadmap",
        ),
        help=(
            "eligible artifact type; repeat as needed "
            "(default: decision, handoff, plan, roadmap)"
        ),
    )
    graphiti.add_argument("--limit-units", type=int, default=0)
    graphiti.add_argument("--retry-ambiguous", action="store_true")
    graphiti.add_argument("--apply", action="store_true")

    status = commands.add_parser("status", help="summarize sink checkpoints")
    _common_workspace(status)
    status.add_argument("--state", default=str(DEFAULT_STATE))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = Path(args.workspace)
    exit_code = 0
    if args.command == "prepare":
        result = prepare_outbox(
            workspace=workspace,
            catalog=Path(args.catalog),
            output_root=Path(args.output_root),
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
            group_id=args.graphiti_group_prefix,
            limit_artifacts=args.limit_artifacts,
        )
    elif args.command == "qdrant":
        qdrant_path = (
            Path(args.qdrant_path)
            if args.qdrant_path
            else (None if args.qdrant_url else DEFAULT_QDRANT_PATH)
        )
        result = qdrant_ingest(
            workspace=workspace,
            catalog=Path(args.catalog),
            outbox=Path(args.outbox),
            state_path=Path(args.state),
            collection=args.collection,
            embedding_model=args.embedding_model,
            local_path=qdrant_path,
            url=args.qdrant_url,
            api_key_env=args.api_key_env,
            batch_size=args.batch_size,
            limit_units=args.limit_units,
            apply=args.apply,
        )
    elif args.command == "qdrant-reconcile":
        qdrant_path = (
            Path(args.qdrant_path)
            if args.qdrant_path
            else (None if args.qdrant_url else DEFAULT_QDRANT_PATH)
        )
        result = qdrant_reconcile(
            workspace=workspace,
            catalog=Path(args.catalog),
            collection=args.collection,
            local_path=qdrant_path,
            url=args.qdrant_url,
            api_key_env=args.api_key_env,
            batch_size=args.batch_size,
            apply=args.apply,
        )
    elif args.command == "search":
        qdrant_path = (
            Path(args.qdrant_path)
            if args.qdrant_path
            else (None if args.qdrant_url else DEFAULT_QDRANT_PATH)
        )
        result = qdrant_search(
            workspace=workspace,
            catalog=Path(args.catalog),
            local_path=qdrant_path,
            url=args.qdrant_url,
            api_key_env=args.api_key_env,
            collection=args.collection,
            embedding_model=args.embedding_model,
            query=args.query,
            limit=args.limit,
            current_only=not args.include_history,
            project=args.project,
            artifact_type=args.artifact_type,
            authority_class=args.authority_class,
            repository=args.repository,
            lifecycle_hint=args.lifecycle_hint,
        )
    elif args.command == "graphiti-check":
        if args.timeout_seconds <= 0:
            raise IngestionError("timeout_seconds must be positive")
        result = graphiti_readiness(
            host=args.host,
            port=args.port,
            password_env=args.password_env,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            embedding_base_url=args.embedding_base_url,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            api_key_env=args.api_key_env,
            probe_models=args.probe_models,
            timeout_seconds=args.timeout_seconds,
        )
        if not result["ready"]:
            exit_code = 2
    elif args.command == "graphiti":
        result = graphiti_ingest(
            workspace=workspace,
            outbox=Path(args.outbox),
            state_path=Path(args.state),
            host=args.host,
            port=args.port,
            password_env=args.password_env,
            database=args.database,
            group_id=args.group_prefix,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            embedding_base_url=args.embedding_base_url,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            api_key_env=args.api_key_env,
            structured_output_mode=args.structured_output_mode,
            instructions_file=(
                Path(args.instructions_file) if args.instructions_file else None
            ),
            limit_units=args.limit_units,
            retry_ambiguous=args.retry_ambiguous,
            apply=args.apply,
            artifact_types=args.artifact_type,
        )
    elif args.command == "status":
        state = IngestionState(Path(args.state), workspace.expanduser().resolve())
        try:
            result = {
                "schema_version": STATE_SCHEMA_VERSION,
                "state": str(state.path),
                "checkpoints": state.summary(),
            }
        finally:
            state.close()
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        IngestionError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
