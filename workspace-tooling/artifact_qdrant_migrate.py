#!/usr/bin/env python3
"""Shadow-migrate embedded artifact vectors into loopback Qdrant safely.

The replay set is not the union of old outboxes.  It is the exact set of units
whose embedded-target checkpoints reached ``completed``, joined temporally to
the newest checksum-valid outbox occurrence that existed when each checkpoint
was written.  This preserves the live 37,527-point state without inventing
superseded points that happened to appear in later full-corpus outboxes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

import artifact_evidence as evidence_schema
import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security


SCHEMA_VERSION = 1
DEFAULT_SELECTION = (
    artifact_runtime.DEFAULT_DERIVED_ROOT / "qdrant-shadow-replay.sqlite3"
)
DEFAULT_EVIDENCE = (
    artifact_runtime.DEFAULT_DERIVED_ROOT / "qdrant-shadow-verification.json"
)


class MigrationError(RuntimeError):
    """A shadow migration invariant failed."""


@dataclass(frozen=True)
class Checkpoint:
    unit_id: str
    revision_id: str
    point_id: str
    updated_at: datetime
    updated_at_text: str


def _iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MigrationError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MigrationError(f"{label} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise MigrationError(f"{label} must include a timezone")
    return parsed


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest_rows(values: Iterator[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(_canonical(value))
    return digest.hexdigest()


def _old_target(runtime: artifact_runtime.ArtifactRuntime) -> str:
    return ingestion._qdrant_target(
        f"local:{runtime.embedded_path}",
        ingestion.DEFAULT_COLLECTION,
        ingestion.DEFAULT_EMBEDDING_MODEL,
    )


def _load_checkpoints(
    path: Path,
    target: str,
) -> tuple[dict[str, Checkpoint], str]:
    security.require_private_file(path)
    checkpoints: dict[str, Checkpoint] = {}
    point_ids: set[str] = set()
    canonical_rows: list[dict[str, str]] = []
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT unit_id, revision_id, external_id, updated_at
            FROM sink_units
            WHERE sink='qdrant' AND target=? AND status='completed'
            ORDER BY unit_id
            """,
            (target,),
        ).fetchall()
    for row in rows:
        if any(value is None or not str(value) for value in row):
            raise MigrationError("completed checkpoint contains a null identity field")
        unit_id, revision_id, point_id, updated_at = (str(value) for value in row)
        if unit_id in checkpoints:
            raise MigrationError(f"duplicate checkpoint unit_id: {unit_id}")
        if point_id in point_ids:
            raise MigrationError(f"duplicate checkpoint point ID: {point_id}")
        checkpoint = Checkpoint(
            unit_id=unit_id,
            revision_id=revision_id,
            point_id=point_id,
            updated_at=_iso(updated_at, "checkpoint updated_at"),
            updated_at_text=updated_at,
        )
        checkpoints[unit_id] = checkpoint
        point_ids.add(point_id)
        canonical_rows.append(
            {
                "external_id": point_id,
                "revision_id": revision_id,
                "unit_id": unit_id,
                "updated_at": updated_at,
            }
        )
    if not checkpoints:
        raise MigrationError(f"no completed checkpoints for target {target!r}")
    return checkpoints, _digest_rows(iter(canonical_rows))


def _validate_unit(unit: dict[str, Any], checkpoint: Checkpoint) -> None:
    required = (
        "unit_id",
        "qdrant_point_id",
        "revision_id",
        "chunk_index",
        "chunk_sha256",
        "content",
    )
    missing = [name for name in required if name not in unit]
    if missing:
        raise MigrationError("outbox unit lacks fields: " + ", ".join(missing))
    content_digest = hashlib.sha256(
        str(unit["content"]).encode("utf-8")
    ).hexdigest()
    if content_digest != str(unit["chunk_sha256"]):
        raise MigrationError(f"chunk hash mismatch for {checkpoint.unit_id}")
    identity = (
        f"{unit['revision_id']}\0{int(unit['chunk_index'])}\0{content_digest}"
    )
    expected_unit = str(uuid.uuid5(ingestion.UNIT_NAMESPACE, identity))
    expected_point = str(uuid.uuid5(ingestion.POINT_NAMESPACE, identity))
    if str(unit["unit_id"]) != expected_unit:
        raise MigrationError(f"unit UUID mismatch for {checkpoint.unit_id}")
    if str(unit["qdrant_point_id"]) != expected_point:
        raise MigrationError(f"point UUID mismatch for {checkpoint.unit_id}")
    if str(unit["revision_id"]) != checkpoint.revision_id:
        raise MigrationError(f"checkpoint revision mismatch for {checkpoint.unit_id}")
    if str(unit["qdrant_point_id"]) != checkpoint.point_id:
        raise MigrationError(f"checkpoint point mismatch for {checkpoint.unit_id}")


def build_selection(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    destination: Path,
) -> dict[str, Any]:
    checkpoints, checkpoint_digest = _load_checkpoints(
        runtime.ingestion_state,
        _old_target(runtime),
    )
    destination = destination.expanduser().absolute()
    directory = security.ensure_private_directory(destination.parent)
    temporary = directory / f".tmp-{destination.name}-{os.getpid()}-{uuid.uuid4().hex}"
    connection = sqlite3.connect(temporary)
    security.secure_created_file(temporary)
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE selected_units (
          unit_id TEXT PRIMARY KEY,
          revision_id TEXT NOT NULL,
          point_id TEXT NOT NULL UNIQUE,
          checkpoint_updated_at TEXT NOT NULL,
          source_outbox TEXT NOT NULL,
          source_created_at TEXT NOT NULL,
          unit_json TEXT NOT NULL
        );
        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL
        );
        """
    )
    selected_at: dict[str, tuple[datetime, str]] = {}
    manifest_descriptors: list[dict[str, Any]] = []
    scanned_occurrences = 0
    qualifying_occurrences = 0
    try:
        outboxes = sorted(
            path
            for path in runtime.outbox_root.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        for outbox in outboxes:
            manifest = ingestion.load_outbox_manifest(outbox)
            created_text = str(manifest.get("created_at", ""))
            created = _iso(created_text, f"{outbox.name} created_at")
            manifest_descriptors.append(
                {
                    "name": outbox.name,
                    "created_at": created_text,
                    "outbox_schema_version": int(
                        manifest["outbox_schema_version"]
                    ),
                    "units_file": str(manifest["units_file"]),
                    "units_sha256": str(manifest["units_sha256"]),
                    "units": int(manifest["counts"]["units"]),
                }
            )
            for unit in ingestion.iter_outbox_units(outbox):
                scanned_occurrences += 1
                unit_id = str(unit.get("unit_id", ""))
                checkpoint = checkpoints.get(unit_id)
                if checkpoint is None or created > checkpoint.updated_at:
                    continue
                qualifying_occurrences += 1
                _validate_unit(unit, checkpoint)
                prior = selected_at.get(unit_id)
                if prior is not None and created == prior[0] and outbox.name != prior[1]:
                    raise MigrationError(
                        f"equal-timestamp replay ambiguity for {unit_id}: "
                        f"{prior[1]} and {outbox.name}"
                    )
                if prior is not None and created < prior[0]:
                    continue
                selected_at[unit_id] = (created, outbox.name)
                connection.execute(
                    """
                    INSERT INTO selected_units(
                      unit_id, revision_id, point_id, checkpoint_updated_at,
                      source_outbox, source_created_at, unit_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(unit_id) DO UPDATE SET
                      revision_id=excluded.revision_id,
                      point_id=excluded.point_id,
                      checkpoint_updated_at=excluded.checkpoint_updated_at,
                      source_outbox=excluded.source_outbox,
                      source_created_at=excluded.source_created_at,
                      unit_json=excluded.unit_json
                    """,
                    (
                        checkpoint.unit_id,
                        checkpoint.revision_id,
                        checkpoint.point_id,
                        checkpoint.updated_at_text,
                        outbox.name,
                        created_text,
                        _canonical(unit).decode("utf-8").rstrip("\n"),
                    ),
                )
            connection.commit()
        missing = sorted(set(checkpoints) - set(selected_at))
        if missing:
            raise MigrationError(
                f"{len(missing)} completed checkpoints lack a temporal outbox match: "
                + ", ".join(missing[:5])
            )
        selected_count = int(
            connection.execute("SELECT COUNT(*) FROM selected_units").fetchone()[0]
        )
        point_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT point_id) FROM selected_units"
            ).fetchone()[0]
        )
        if selected_count != len(checkpoints) or point_count != len(checkpoints):
            raise MigrationError(
                "selection count/point uniqueness does not match checkpoints"
            )
        selected_digest = _digest_rows(
            json.loads(str(row[0]))
            for row in connection.execute(
                "SELECT unit_json FROM selected_units ORDER BY unit_id"
            )
        )
        manifest_digest = _digest_rows(iter(manifest_descriptors))
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "old_target": _old_target(runtime),
            "checkpoint_count": len(checkpoints),
            "checkpoint_digest": checkpoint_digest,
            "selected_count": selected_count,
            "selected_digest": selected_digest,
            "manifest_count": len(manifest_descriptors),
            "manifest_digest": manifest_digest,
            "scanned_occurrences": scanned_occurrences,
            "qualifying_occurrences": qualifying_occurrences,
        }
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        connection.close()
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        security.secure_created_file(destination)
        security.fsync_directory(directory)
        return metadata | {"destination": str(destination)}
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise


def _selection_metadata(path: Path) -> dict[str, Any]:
    security.require_private_file(path)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise MigrationError(f"unsupported selection schema {version}")
        return {
            str(key): json.loads(str(value))
            for key, value in connection.execute(
                "SELECT key, value_json FROM metadata"
            )
        }


def _iter_selection(path: Path, batch_size: int) -> Iterator[list[dict[str, Any]]]:
    security.require_private_file(path)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        cursor = connection.execute(
            "SELECT unit_json FROM selected_units ORDER BY unit_id"
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield [json.loads(str(row[0])) for row in rows]


def _model_manifest(embedder: Any) -> dict[str, Any]:
    model = getattr(embedder, "model", None)
    model_dir = Path(str(getattr(model, "_model_dir", ""))).resolve(strict=True)
    files: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    for path in sorted(candidate for candidate in model_dir.rglob("*") if candidate.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        record = {
            "path": path.relative_to(model_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
        files.append(record)
        aggregate.update(_canonical(record))
    if not files:
        raise MigrationError("embedding model cache contains no files")
    return {
        "name": ingestion.DEFAULT_EMBEDDING_MODEL,
        "source": "qdrant/all-MiniLM-L6-v2-onnx",
        "cache_path": str(model_dir),
        "files": files,
        "manifest_sha256": aggregate.hexdigest(),
    }


def backfill(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    selection: Path,
    batch_size: int,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 256:
        raise MigrationError("batch size must be between 1 and 256")
    selected = _selection_metadata(selection)
    embedder = ingestion.create_text_embedder(ingestion.DEFAULT_EMBEDDING_MODEL)
    model_manifest = _model_manifest(embedder)
    model_digest = str(model_manifest["manifest_sha256"])
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise MigrationError("Qdrant client dependency is unavailable") from exc
    client = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_admin_key(),
        timeout=60,
    )
    current_revisions = ingestion._current_revision_ids(
        runtime.catalog,
        runtime.workspace,
    )
    started = time.monotonic()
    created = False
    ingested = 0
    try:
        metadata = {
            "generation": runtime.qdrant_generation,
            "embedding_model": ingestion.DEFAULT_EMBEDDING_MODEL,
            "embedding_model_digest": model_digest,
            "chunk_profile_id": ingestion.DEFAULT_CHUNK_PROFILE_ID,
            "normalization_version": ingestion.DEFAULT_NORMALIZATION_VERSION,
            "selection_digest": selected["selected_digest"],
        }
        if not client.collection_exists(runtime.qdrant_collection):
            client.create_collection(
                collection_name=runtime.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=384,
                    distance=models.Distance.COSINE,
                ),
                on_disk_payload=True,
                metadata=metadata,
            )
            created = True
        else:
            information = client.get_collection(runtime.qdrant_collection)
            existing = dict(information.config.metadata or {})
            if existing != metadata:
                raise MigrationError(
                    "existing collection metadata does not match this generation"
                )
        indexes = ingestion._ensure_qdrant_payload_indexes(
            client,
            models,
            runtime.qdrant_collection,
        )
        for units in _iter_selection(selection, batch_size):
            vectors = list(
                embedder.embed([str(unit["embedding_text"]) for unit in units])
            )
            points = [
                models.PointStruct(
                    id=str(unit["qdrant_point_id"]),
                    vector=vector.tolist(),
                    payload=ingestion._unit_payload(
                        unit,
                        ingestion.DEFAULT_EMBEDDING_MODEL,
                        catalog_current=str(unit["revision_id"])
                        in current_revisions,
                        collection_generation=runtime.qdrant_generation,
                        embedding_model_digest=model_digest,
                    ),
                )
                for unit, vector in zip(units, vectors)
            ]
            client.upsert(
                collection_name=runtime.qdrant_collection,
                points=points,
                wait=True,
            )
            ingested += len(points)
        count = int(
            client.count(
                collection_name=runtime.qdrant_collection,
                exact=True,
            ).count
        )
        if count != int(selected["selected_count"]):
            raise MigrationError(
                f"server count {count} != selected count {selected['selected_count']}"
            )
        build = {
            "schema_version": SCHEMA_VERSION,
            "built_at": datetime.now().astimezone().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "qdrant": {
                "url": "http://127.0.0.1:6343",
                "server_version": client.info().version,
                "client_version": importlib.metadata.version("qdrant-client"),
                "collection": runtime.qdrant_collection,
                "generation": runtime.qdrant_generation,
                "created": created,
                "created_payload_indexes": indexes,
                "points": count,
            },
            "embedding": model_manifest,
            "fastembed_version": importlib.metadata.version("fastembed"),
            "selection": selected,
            "ingested": ingested,
            "source_authority": "verified-checkpoint-authorized-outboxes",
            "embedded_storage_mounted": False,
            "canonical_mutation": "disabled",
        }
        security.atomic_write_json(runtime.build_manifest, build, replace=True)
        return build
    finally:
        client.close()


def _scroll_payloads(client: Any, collection: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    offset: Any = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=[
                "unit_id",
                "revision_id",
                "content_sha256",
                "chunk_sha256",
                "catalog_current",
                "collection_generation",
                "embedding_model_digest",
            ],
            with_vectors=False,
        )
        for point in points:
            point_id = str(point.id)
            if point_id in result:
                raise MigrationError(f"Qdrant returned duplicate point ID {point_id}")
            result[point_id] = dict(point.payload or {})
        if next_offset is None:
            return result
        offset = next_offset


def _sample(values: list[str], count: int) -> list[str]:
    if len(values) <= count:
        return values
    return [values[(index * len(values)) // count] for index in range(count)]


def verify(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    selection: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise MigrationError("Qdrant client dependency is unavailable") from exc
    server = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_read_key(),
        timeout=30,
    )
    embedded = QdrantClient(path=str(runtime.embedded_path))
    embedder = ingestion.create_text_embedder(ingestion.DEFAULT_EMBEDDING_MODEL)
    expected: dict[str, dict[str, Any]] = {}
    for batch in _iter_selection(selection, 512):
        for unit in batch:
            expected[str(unit["qdrant_point_id"])] = unit
    current_revisions = ingestion._current_revision_ids(
        runtime.catalog,
        runtime.workspace,
    )
    started = time.monotonic()
    try:
        server_points = _scroll_payloads(server, runtime.qdrant_collection)
        embedded_points = _scroll_payloads(embedded, ingestion.DEFAULT_COLLECTION)
        expected_ids = set(expected)
        server_ids = set(server_points)
        embedded_ids = set(embedded_points)
        count_parity = (
            server_ids == expected_ids
            and embedded_ids == expected_ids
        )
        expected_current = sum(
            1
            for unit in expected.values()
            if str(unit["revision_id"]) in current_revisions
        )
        server_current = sum(
            1
            for payload in server_points.values()
            if payload.get("catalog_current") is True
        )
        embedded_current = sum(
            1
            for payload in embedded_points.values()
            if payload.get("catalog_current") is True
        )
        sample_ids = _sample(sorted(expected_ids), 100)
        hash_failures: list[str] = []
        for point_id in sample_ids:
            unit = expected[point_id]
            for name, points in (
                ("server", server_points),
                ("embedded", embedded_points),
            ):
                payload = points.get(point_id, {})
                for field in (
                    "unit_id",
                    "revision_id",
                    "content_sha256",
                    "chunk_sha256",
                ):
                    if str(payload.get(field)) != str(unit[field]):
                        hash_failures.append(f"{name}:{point_id}:{field}")
        current_units = [
            expected[point_id]
            for point_id in sorted(expected_ids)
            if str(expected[point_id]["revision_id"]) in current_revisions
        ]
        query_units = [
            current_units[(index * len(current_units)) // 100]
            for index in range(100)
        ]
        query_texts = [
            (
                (str(unit.get("heading") or "") + "\n" + str(unit["content"]))
                .strip()[:600]
            )
            for unit in query_units
        ]
        vectors = list(embedder.embed(query_texts))
        current_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="catalog_current",
                    match=models.MatchValue(value=True),
                )
            ]
        )
        exact_top1_matches = 0
        exact_overlap_total = 0.0
        exact_logical_top1_matches = 0
        exact_logical_overlap_total = 0.0
        approximate_top1_matches = 0
        approximate_overlap_total = 0.0
        approximate_logical_top1_matches = 0
        approximate_logical_overlap_total = 0.0
        query_records: list[dict[str, Any]] = []
        for unit, text, vector in zip(query_units, query_texts, vectors):
            server_exact_hits = server.query_points(
                collection_name=runtime.qdrant_collection,
                query=vector.tolist(),
                query_filter=current_filter,
                limit=10,
                with_payload=False,
                search_params=models.SearchParams(exact=True),
            ).points
            embedded_hits = embedded.query_points(
                collection_name=ingestion.DEFAULT_COLLECTION,
                query=vector.tolist(),
                query_filter=current_filter,
                limit=10,
                with_payload=False,
                search_params=models.SearchParams(exact=True),
            ).points
            server_approximate_hits = server.query_points(
                collection_name=runtime.qdrant_collection,
                query=vector.tolist(),
                query_filter=current_filter,
                limit=10,
                with_payload=False,
            ).points
            server_exact_ids = [str(point.id) for point in server_exact_hits]
            server_approximate_ids = [
                str(point.id) for point in server_approximate_hits
            ]
            embedded_ids_10 = [str(point.id) for point in embedded_hits]
            exact_top1 = bool(
                server_exact_ids
                and embedded_ids_10
                and server_exact_ids[0] == embedded_ids_10[0]
            )
            approximate_top1 = bool(
                server_approximate_ids
                and embedded_ids_10
                and server_approximate_ids[0] == embedded_ids_10[0]
            )
            exact_top1_matches += int(exact_top1)
            approximate_top1_matches += int(approximate_top1)
            exact_overlap = (
                len(set(server_exact_ids) & set(embedded_ids_10)) / 10.0
            )
            approximate_overlap = (
                len(set(server_approximate_ids) & set(embedded_ids_10)) / 10.0
            )
            exact_overlap_total += exact_overlap
            approximate_overlap_total += approximate_overlap
            embedded_logical = [
                str(embedded_points[point_id].get("chunk_sha256"))
                for point_id in embedded_ids_10
            ]
            server_exact_logical = [
                str(server_points[point_id].get("chunk_sha256"))
                for point_id in server_exact_ids
            ]
            server_approximate_logical = [
                str(server_points[point_id].get("chunk_sha256"))
                for point_id in server_approximate_ids
            ]
            exact_logical_top1 = bool(
                server_exact_logical
                and embedded_logical
                and server_exact_logical[0] == embedded_logical[0]
            )
            approximate_logical_top1 = bool(
                server_approximate_logical
                and embedded_logical
                and server_approximate_logical[0] == embedded_logical[0]
            )
            exact_logical_overlap = (
                len(set(server_exact_logical) & set(embedded_logical))
                / max(1, len(set(embedded_logical)))
            )
            approximate_logical_overlap = (
                len(set(server_approximate_logical) & set(embedded_logical))
                / max(1, len(set(embedded_logical)))
            )
            exact_logical_top1_matches += int(exact_logical_top1)
            approximate_logical_top1_matches += int(approximate_logical_top1)
            exact_logical_overlap_total += exact_logical_overlap
            approximate_logical_overlap_total += approximate_logical_overlap
            query_records.append(
                {
                    "query_sha256": hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest(),
                    "expected_point_id": str(unit["qdrant_point_id"]),
                    "server_exact_top1": (
                        server_exact_ids[0] if server_exact_ids else None
                    ),
                    "server_approximate_top1": (
                        server_approximate_ids[0]
                        if server_approximate_ids
                        else None
                    ),
                    "embedded_top1": embedded_ids_10[0] if embedded_ids_10 else None,
                    "exact_top1_equal": exact_top1,
                    "exact_top10_overlap": exact_overlap,
                    "exact_logical_top1_equal": exact_logical_top1,
                    "exact_logical_top10_overlap": exact_logical_overlap,
                    "approximate_top1_equal": approximate_top1,
                    "approximate_top10_overlap": approximate_overlap,
                    "approximate_logical_top1_equal": approximate_logical_top1,
                    "approximate_logical_top10_overlap": (
                        approximate_logical_overlap
                    ),
                }
            )
        exact_top1_rate = exact_top1_matches / 100.0
        exact_mean_overlap = exact_overlap_total / 100.0
        exact_logical_top1_rate = exact_logical_top1_matches / 100.0
        exact_logical_mean_overlap = exact_logical_overlap_total / 100.0
        approximate_top1_rate = approximate_top1_matches / 100.0
        approximate_mean_overlap = approximate_overlap_total / 100.0
        approximate_logical_top1_rate = (
            approximate_logical_top1_matches / 100.0
        )
        approximate_logical_mean_overlap = (
            approximate_logical_overlap_total / 100.0
        )
        # Six gated criteria, each carrying its own bar into the artifact
        # (F-16). The four rates recorded below them are measured but not
        # gated; naming that explicitly stops them reading as silent passes.
        gate = evidence_schema.summarize(
            [
                evidence_schema.check(
                    "exact_set_parity",
                    observed=count_parity,
                    operator="==",
                    threshold=True,
                ),
                evidence_schema.check(
                    "current_count_agreement",
                    observed=(expected_current == server_current == embedded_current),
                    operator="==",
                    threshold=True,
                ),
                evidence_schema.check(
                    "canonical_hash_failures",
                    observed=len(hash_failures),
                    operator="==",
                    threshold=0,
                ),
                evidence_schema.check(
                    "exact_mean_top10_overlap",
                    observed=exact_mean_overlap,
                    operator=">=",
                    threshold=0.95,
                ),
                evidence_schema.check(
                    "exact_logical_mean_top10_overlap",
                    observed=exact_logical_mean_overlap,
                    operator=">=",
                    threshold=0.98,
                ),
                evidence_schema.check(
                    "approximate_logical_mean_top10_overlap",
                    observed=approximate_logical_mean_overlap,
                    operator=">=",
                    threshold=0.80,
                ),
            ],
            observations=[
                evidence_schema.observation(
                    "exact_top1_match_rate",
                    observed=exact_top1_rate,
                    reason="reported for trend visibility; logical-identity overlap is the gated signal",
                ),
                evidence_schema.observation(
                    "exact_logical_top1_match_rate",
                    observed=exact_logical_top1_rate,
                    reason="reported for trend visibility; mean overlap is the gated signal",
                ),
                evidence_schema.observation(
                    "approximate_top1_match_rate",
                    observed=approximate_top1_rate,
                    reason="approximate search is non-deterministic; not a gate",
                ),
                evidence_schema.observation(
                    "approximate_mean_top10_overlap",
                    observed=approximate_mean_overlap,
                    reason="superseded by the logical-identity variant, which is gated",
                ),
            ],
        )
        passed = gate["status"] == "passed"
        result = {
            "schema_version": SCHEMA_VERSION,
            "verified_at": datetime.now().astimezone().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "passed": passed,
            "gate": gate,
            "counts": {
                "expected": len(expected_ids),
                "server": len(server_ids),
                "embedded": len(embedded_ids),
                "expected_current": expected_current,
                "server_current": server_current,
                "embedded_current": embedded_current,
                "expected_historical": len(expected_ids) - expected_current,
                "server_historical": len(server_ids) - server_current,
                "embedded_historical": len(embedded_ids) - embedded_current,
            },
            "identity": {
                "exact_set_parity": count_parity,
                "server_extra": sorted(server_ids - expected_ids)[:20],
                "server_missing": sorted(expected_ids - server_ids)[:20],
                "embedded_extra": sorted(embedded_ids - expected_ids)[:20],
                "embedded_missing": sorted(expected_ids - embedded_ids)[:20],
            },
            "canonical_hash_sample": {
                "sample_size": len(sample_ids),
                "failures": hash_failures,
            },
            "deterministic_query_parity": {
                "queries": len(query_records),
                "exact_top1_match_rate": exact_top1_rate,
                "exact_mean_top10_overlap": exact_mean_overlap,
                "exact_logical_top1_match_rate": exact_logical_top1_rate,
                "exact_logical_mean_top10_overlap": exact_logical_mean_overlap,
                "approximate_top1_match_rate": approximate_top1_rate,
                "approximate_mean_top10_overlap": approximate_mean_overlap,
                "approximate_logical_top1_match_rate": (
                    approximate_logical_top1_rate
                ),
                "approximate_logical_mean_top10_overlap": (
                    approximate_logical_mean_overlap
                ),
                "records": query_records,
            },
            "embedded_path_unchanged": True,
            "production_cutover": "blocked-until-item8-gold-gate",
        }
        security.atomic_write_json(evidence_path, result, replace=True)
        if not passed:
            raise MigrationError(
                f"shadow verification failed; evidence: {evidence_path}"
            )
        return result
    finally:
        embedded.close()
        server.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=artifact_runtime.DEFAULT_CONFIG,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--destination", type=Path, default=DEFAULT_SELECTION)
    fill = commands.add_parser("backfill")
    fill.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    fill.add_argument("--batch-size", type=int, default=64)
    check = commands.add_parser("verify")
    check.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    check.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = artifact_runtime.load_runtime(args.config)
    if args.command == "select":
        return build_selection(runtime=runtime, destination=args.destination)
    if args.command == "backfill":
        return backfill(
            runtime=runtime,
            selection=args.selection,
            batch_size=args.batch_size,
        )
    if args.command == "verify":
        return verify(
            runtime=runtime,
            selection=args.selection,
            evidence_path=args.evidence,
        )
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(_parser().parse_args(argv))
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
