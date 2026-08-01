#!/usr/bin/env python3
"""Build a Qdrant retrieval generation from an exact span manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sqlite3
from contextlib import closing
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import artifact_runtime
import artifact_retrieval
import artifact_security as security
import artifact_span_generation as spans


SCHEMA_VERSION = 1
DEFAULT_COLLECTION = "personal_artifact_spans_p20260721v2"
DEFAULT_STATE = (
    artifact_runtime.DEFAULT_DERIVED_ROOT / "artifact-retrieval-migration-p20260721v2.sqlite3"
)
DEFAULT_EVIDENCE = (
    artifact_runtime.DEFAULT_DERIVED_ROOT / "artifact-retrieval-migration-p20260721v2.json"
)


class RetrievalMigrationError(RuntimeError):
    """The manifest-to-Qdrant migration violated a release invariant."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_metadata(path: Path) -> dict[str, Any]:
    security.require_private_file(path)
    with closing(sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1",
        uri=True,
    )) as connection, connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != spans.SCHEMA_VERSION:
            raise RetrievalMigrationError(
                f"unsupported span manifest schema {version}"
            )
        metadata = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute(
                "SELECT key, value_json FROM metadata ORDER BY key"
            )
        }
        count = int(connection.execute("SELECT COUNT(*) FROM spans").fetchone()[0])
        ready = int(
            connection.execute(
                "SELECT COUNT(*) FROM spans WHERE ready=1 AND catalog_current=1"
            ).fetchone()[0]
        )
    if count != ready or count != int(metadata["spans"]):
        raise RetrievalMigrationError(
            f"manifest readiness/count mismatch: total={count}, ready={ready}"
        )
    metadata["file_sha256"] = _file_sha256(path)
    return metadata


def _verified_model_snapshot(
    path: Path,
    *,
    expected_digest: str,
) -> tuple[list[dict[str, Any]], str]:
    try:
        observed_digest, files = security.private_tree_manifest(path)
    except security.PrivateStateError as exc:
        raise RetrievalMigrationError(
            f"embedding model snapshot is unsafe: {exc}"
        ) from exc
    if observed_digest != expected_digest:
        raise RetrievalMigrationError(
            "embedding model snapshot does not match the span manifest"
        )
    return files, observed_digest


def _open_state(
    path: Path,
    *,
    manifest_sha256: str,
    collection: str,
    generation: str,
    model_manifest_digest: str,
    execution_contract_digest: str,
) -> sqlite3.Connection:
    path = path.expanduser().absolute()
    security.ensure_private_directory(path.parent)
    connection = sqlite3.connect(path, timeout=30)
    security.secure_created_file(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS progress (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          last_row_id INTEGER NOT NULL,
          points INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vector_attestations (
          point_id TEXT PRIMARY KEY,
          leaf_sha256 BLOB NOT NULL CHECK(length(leaf_sha256)=32)
        );
        INSERT OR IGNORE INTO progress VALUES (1, 0, 0, '');
        """
    )
    expected = {
        "manifest_sha256": manifest_sha256,
        "collection": collection,
        "generation": generation,
        "model_manifest_digest": model_manifest_digest,
        "execution_contract_digest": execution_contract_digest,
    }
    existing_points = int(
        connection.execute(
            "SELECT points FROM progress WHERE singleton=1"
        ).fetchone()[0]
    )
    for key, value in expected.items():
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key=?",
            (key,),
        ).fetchone()
        if existing is None:
            if (
                key in {
                    "model_manifest_digest",
                    "execution_contract_digest",
                }
                and existing_points
            ):
                connection.close()
                raise RetrievalMigrationError(
                    "non-empty legacy checkpoint lacks a release binding"
                )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (key, value),
            )
        elif str(existing[0]) != value:
            connection.close()
            raise RetrievalMigrationError(
                f"migration state {key} does not match this build"
            )
    attested = int(
        connection.execute(
            "SELECT COUNT(*) FROM vector_attestations"
        ).fetchone()[0]
    )
    if attested != existing_points:
        connection.close()
        raise RetrievalMigrationError(
            "migration checkpoint lacks a complete vector attestation ledger"
        )
    connection.commit()
    return connection


def _contract_digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _verified_published_vectors(
    *,
    client: Any,
    collection: str,
    point_ids: Sequence[str],
    uploaded_vectors: Sequence[Any],
) -> list[tuple[str, bytes]]:
    if len(point_ids) != len(uploaded_vectors):
        raise RetrievalMigrationError(
            "published vector verification received mismatched inputs"
        )
    retrieved = client.retrieve(
        collection_name=collection,
        ids=list(point_ids),
        with_payload=False,
        with_vectors=True,
    )
    by_id = {str(point.id): point for point in retrieved}
    if len(by_id) != len(point_ids) or set(by_id) != set(point_ids):
        raise RetrievalMigrationError(
            "Qdrant did not return the exact published vector batch"
        )
    attestations: list[tuple[str, bytes]] = []
    for point_id, uploaded in zip(point_ids, uploaded_vectors):
        expected = [float(value) for value in uploaded]
        observed = getattr(by_id[point_id], "vector", None)
        try:
            observed_values = [float(value) for value in observed]
        except (TypeError, ValueError) as exc:
            raise RetrievalMigrationError(
                f"Qdrant returned an invalid vector for {point_id}"
            ) from exc
        if (
            len(expected) != artifact_retrieval.EMBEDDING_DIMENSIONS
            or len(observed_values) != artifact_retrieval.EMBEDDING_DIMENSIONS
            or not all(math.isfinite(value) for value in expected)
            or not all(math.isfinite(value) for value in observed_values)
        ):
            raise RetrievalMigrationError(
                f"Qdrant returned a malformed vector for {point_id}"
            )
        expected_norm = math.sqrt(sum(value * value for value in expected))
        observed_norm = math.sqrt(
            sum(value * value for value in observed_values)
        )
        if expected_norm <= 0.0 or observed_norm <= 0.0:
            raise RetrievalMigrationError(
                f"Qdrant returned a zero vector for {point_id}"
            )
        cosine = sum(
            left * right
            for left, right in zip(expected, observed_values)
        ) / (expected_norm * observed_norm)
        if not math.isfinite(cosine) or cosine < 0.99999:
            raise RetrievalMigrationError(
                f"Qdrant vector differs from the bound model for {point_id}"
            )
        attestations.append(
            (
                point_id,
                artifact_retrieval.qdrant_vector_leaf_sha256(
                    point_id,
                    observed_values,
                ),
            )
        )
    return attestations


def _verify_checkpoint_vectors(
    *,
    client: Any,
    collection: str,
    state: sqlite3.Connection,
    batch_size: int = 256,
) -> None:
    cursor = state.execute(
        """
        SELECT point_id, leaf_sha256
        FROM vector_attestations
        ORDER BY point_id
        """
    )
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        expected = {str(row[0]): bytes(row[1]) for row in rows}
        points = client.retrieve(
            collection_name=collection,
            ids=list(expected),
            with_payload=False,
            with_vectors=True,
        )
        observed = {
            str(point.id): artifact_retrieval.qdrant_vector_leaf_sha256(
                str(point.id),
                getattr(point, "vector", None),
            )
            for point in points
        }
        if observed != expected:
            raise RetrievalMigrationError(
                "Qdrant vectors differ from the bound migration checkpoint"
            )


def _checkpoint_vector_digest(
    state: sqlite3.Connection,
) -> tuple[int, str]:
    leaves = [
        bytes(row[0])
        for row in state.execute(
            "SELECT leaf_sha256 FROM vector_attestations"
        )
    ]
    digest = hashlib.sha256()
    for leaf in sorted(leaves):
        digest.update(leaf)
    return len(leaves), digest.hexdigest()


def _iter_spans(
    manifest: Path,
    *,
    after_row_id: int,
    batch_size: int,
) -> Iterator[list[dict[str, Any]]]:
    with closing(sqlite3.connect(
        f"file:{manifest.resolve()}?mode=ro&immutable=1",
        uri=True,
    )) as connection, connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            SELECT *
            FROM spans
            WHERE row_id>? AND ready=1 AND catalog_current=1
            ORDER BY row_id
            """,
            (after_row_id,),
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield [dict(row) for row in rows]


def _collection_metadata(
    metadata: dict[str, Any],
    *,
    manifest_file_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": metadata["generation"],
        "profile_id": metadata["profile_id"],
        "profile_digest": metadata["profile_digest"],
        "span_manifest_digest": metadata["span_manifest_digest"],
        "manifest_file_sha256": manifest_file_sha256,
        "embedding_model": metadata["embedding_model"],
        "embedding_model_manifest_digest": metadata[
            "embedding_model_manifest_digest"
        ],
        "content_payload": False,
        "current_only": True,
    }


def _ensure_indexes(client: Any, models: Any, collection: str) -> list[str]:
    information = client.get_collection(collection)
    keyword = (
        "span_id",
        "artifact_id",
        "revision_id",
        "relative_path",
        "content_sha256",
        "span_sha256",
        "artifact_type",
        "authority_class",
        "lifecycle_hints",
        "source_scope",
        "repository",
        "project",
        "profile_id",
        "profile_digest",
        "collection_generation",
        "span_manifest_digest",
        "embedding_model_digest",
    )
    expected = {
        **{name: "keyword" for name in keyword},
        "ready": "bool",
        "catalog_current": "bool",
    }
    existing = information.payload_schema or {}
    for name, schema in existing.items():
        if name not in expected:
            continue
        data_type = getattr(schema, "data_type", schema)
        observed = str(getattr(data_type, "value", data_type)).casefold()
        if observed != expected[name]:
            raise RetrievalMigrationError(
                f"payload index {name} has type {observed}, "
                f"expected {expected[name]}"
            )
    created: list[str] = []
    for name in keyword:
        if name not in existing:
            client.create_payload_index(
                collection_name=collection,
                field_name=name,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )
            created.append(name)
    for name in ("ready", "catalog_current"):
        if name not in existing:
            client.create_payload_index(
                collection_name=collection,
                field_name=name,
                field_schema=models.PayloadSchemaType.BOOL,
                wait=True,
            )
            created.append(name)
    final_schema = client.get_collection(collection).payload_schema or {}
    for name, expected_type in expected.items():
        schema = final_schema.get(name)
        data_type = getattr(schema, "data_type", schema)
        observed = str(getattr(data_type, "value", data_type)).casefold()
        if observed != expected_type:
            raise RetrievalMigrationError(
                f"payload index {name} is absent or has the wrong type"
            )
    return created


def _migration_contract(
    *,
    batch_size: int,
    inference_batch_size: int,
    threads: int,
) -> dict[str, Any]:
    sources = (
        Path(__file__).resolve(),
        Path(artifact_retrieval.__file__).resolve(),
        Path(spans.__file__).resolve(),
        Path(security.__file__).resolve(),
        Path(artifact_runtime.__file__).resolve(),
    )
    return {
        "implementation_files": {
            path.name: _file_sha256(path) for path in sources
        },
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "fastembed": importlib.metadata.version("fastembed"),
        "numpy": importlib.metadata.version("numpy"),
        "onnxruntime": importlib.metadata.version("onnxruntime"),
        "qdrant_client": importlib.metadata.version("qdrant-client"),
        "tokenizers": importlib.metadata.version("tokenizers"),
        "qdrant_server": artifact_retrieval.QDRANT_SERVER_VERSION,
        "embedding_dimensions": artifact_retrieval.EMBEDDING_DIMENSIONS,
        "execution_parameters": {
            "publication_batch_size": batch_size,
            "inference_batch_size": inference_batch_size,
            "threads": threads,
        },
    }


def migrate(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    manifest: Path,
    model_snapshot: Path,
    collection: str,
    state_path: Path,
    evidence_path: Path,
    batch_size: int,
    inference_batch_size: int,
    threads: int,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 256:
        raise RetrievalMigrationError("batch size must be between 1 and 256")
    if not 1 <= inference_batch_size <= batch_size:
        raise RetrievalMigrationError(
            "inference batch size must be between 1 and the publication batch"
        )
    if not 1 <= threads <= 32:
        raise RetrievalMigrationError("embedding threads must be between 1 and 32")
    metadata = _manifest_metadata(manifest)
    generation = str(metadata["generation"])
    manifest_file_sha = str(metadata["file_sha256"])
    model_files, observed_model_digest = _verified_model_snapshot(
        model_snapshot,
        expected_digest=str(metadata["embedding_model_manifest_digest"]),
    )
    migration_contract = _migration_contract(
        batch_size=batch_size,
        inference_batch_size=inference_batch_size,
        threads=threads,
    )
    migration_contract_digest = _contract_digest(migration_contract)
    state = _open_state(
        state_path,
        manifest_sha256=manifest_file_sha,
        collection=collection,
        generation=generation,
        model_manifest_digest=observed_model_digest,
        execution_contract_digest=migration_contract_digest,
    )
    try:
        from fastembed import TextEmbedding
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        state.close()
        raise RetrievalMigrationError("retrieval dependencies are unavailable") from exc
    embedder = TextEmbedding(
        model_name=str(metadata["embedding_model"]),
        specific_model_path=str(model_snapshot),
        local_files_only=True,
        threads=threads,
    )
    client = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_admin_key(),
        timeout=90,
    )
    collection_meta = _collection_metadata(
        metadata,
        manifest_file_sha256=manifest_file_sha,
    )
    started = time.monotonic()
    created = False
    indexes: list[str] = []
    try:
        if str(client.info().version) != artifact_retrieval.QDRANT_SERVER_VERSION:
            raise RetrievalMigrationError(
                "Qdrant server version does not match the retrieval contract"
            )
        if not client.collection_exists(collection):
            client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=artifact_retrieval.EMBEDDING_DIMENSIONS,
                    distance=models.Distance.COSINE,
                ),
                on_disk_payload=True,
                metadata=collection_meta,
            )
            created = True
        else:
            information = client.get_collection(collection)
            if dict(information.config.metadata or {}) != collection_meta:
                raise RetrievalMigrationError(
                    "existing collection metadata differs from the manifest"
                )
            vector = information.config.params.vectors
            distance = getattr(vector, "distance", None)
            distance_value = getattr(distance, "value", distance)
            if (
                isinstance(vector, dict)
                or getattr(vector, "size", None)
                != artifact_retrieval.EMBEDDING_DIMENSIONS
                or str(distance_value).casefold() != "cosine"
            ):
                raise RetrievalMigrationError(
                    "existing collection vector space is not 384-dimensional cosine"
                )
        indexes = _ensure_indexes(client, models, collection)
        progress = state.execute(
            "SELECT last_row_id, points FROM progress WHERE singleton=1"
        ).fetchone()
        last_row_id, processed = int(progress[0]), int(progress[1])
        _verify_checkpoint_vectors(
            client=client,
            collection=collection,
            state=state,
        )
        for batch in _iter_spans(
            manifest,
            after_row_id=last_row_id,
            batch_size=batch_size,
        ):
            vectors = list(
                embedder.embed(
                    [str(row["embedding_text"]) for row in batch],
                    batch_size=inference_batch_size,
                )
            )
            if len(vectors) != len(batch):
                raise RetrievalMigrationError(
                    "embedding model returned the wrong batch size"
                )
            points = []
            uploaded_vectors = []
            point_ids = []
            for row, vector in zip(batch, vectors):
                payload = {
                    "span_id": row["span_id"],
                    "artifact_id": row["artifact_id"],
                    "revision_id": row["revision_id"],
                    "relative_path": row["relative_path"],
                    "content_sha256": row["content_sha256"],
                    "span_sha256": row["span_sha256"],
                    "byte_start": int(row["byte_start"]),
                    "byte_end": int(row["byte_end"]),
                    "line_start": int(row["line_start"]),
                    "line_end": int(row["line_end"]),
                    "artifact_type": row["artifact_type"],
                    "authority_class": row["authority_class"],
                    "lifecycle_hints": json.loads(row["lifecycle_hints_json"]),
                    "source_scope": row["source_scope"],
                    "repository": row["repository"],
                    "project": row["project"],
                    "profile_id": row["profile_id"],
                    "profile_digest": row["profile_digest"],
                    "collection_generation": row["collection_generation"],
                    "span_manifest_digest": metadata["span_manifest_digest"],
                    "embedding_model_digest": metadata[
                        "embedding_model_manifest_digest"
                    ],
                    "ready": True,
                    "catalog_current": True,
                }
                if "content" in payload or "embedding_text" in payload:
                    raise RetrievalMigrationError(
                        "body content must not enter the Qdrant payload"
                    )
                points.append(
                    models.PointStruct(
                        id=str(row["point_id"]),
                        vector=vector.tolist(),
                        payload=payload,
                    )
                )
                point_ids.append(str(row["point_id"]))
                uploaded_vectors.append(vector)
            client.upsert(
                collection_name=collection,
                points=points,
                wait=True,
            )
            attestations = _verified_published_vectors(
                client=client,
                collection=collection,
                point_ids=point_ids,
                uploaded_vectors=uploaded_vectors,
            )
            last_row_id = int(batch[-1]["row_id"])
            processed += len(batch)
            with state:
                state.executemany(
                    """
                    INSERT INTO vector_attestations(
                      point_id, leaf_sha256
                    ) VALUES (?, ?)
                    """,
                    attestations,
                )
                state.execute(
                    """
                    UPDATE progress
                    SET last_row_id=?, points=?, updated_at=?
                    WHERE singleton=1
                    """,
                    (
                        last_row_id,
                        processed,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        expected = int(metadata["spans"])
        observed = int(
            client.count(collection_name=collection, exact=True).count
        )
        if observed != expected:
            raise RetrievalMigrationError(
                f"Qdrant point count {observed} != manifest spans {expected}"
            )
        point_set = artifact_retrieval.verify_qdrant_point_set(
            client=client,
            collection=collection,
            manifest=manifest,
        )
        attested_points, attested_vector_digest = (
            _checkpoint_vector_digest(state)
        )
        if (
            attested_points != expected
            or attested_vector_digest
            != point_set["vector_set_sha256"]
        ):
            raise RetrievalMigrationError(
                "final Qdrant vectors differ from the model-bound "
                "checkpoint ledger"
            )
        sample_rows: list[tuple[Any, ...]]
        with closing(sqlite3.connect(
            f"file:{manifest.resolve()}?mode=ro&immutable=1",
            uri=True,
        )) as connection, connection:
            sample_rows = connection.execute(
                """
                SELECT point_id, span_id, revision_id, span_sha256
                FROM spans
                WHERE row_id IN (
                  SELECT 1
                  UNION SELECT CAST((SELECT MAX(row_id) FROM spans) / 2 AS INTEGER)
                  UNION SELECT MAX(row_id) FROM spans
                )
                ORDER BY point_id
                """
            ).fetchall()
        payload_failures: list[str] = []
        for point_id, span_id, revision_id, span_sha in sample_rows:
            points = client.retrieve(
                collection_name=collection,
                ids=[str(point_id)],
                with_payload=True,
                with_vectors=False,
            )
            if len(points) != 1:
                payload_failures.append(f"{point_id}:missing")
                continue
            payload = dict(points[0].payload or {})
            expected_payload = {
                "span_id": str(span_id),
                "revision_id": str(revision_id),
                "span_sha256": str(span_sha),
            }
            for key, value in expected_payload.items():
                if payload.get(key) != value:
                    payload_failures.append(f"{point_id}:{key}")
            if "content" in payload or "embedding_text" in payload:
                payload_failures.append(f"{point_id}:content-leak")
        if payload_failures:
            raise RetrievalMigrationError(
                "Qdrant payload verification failed: "
                + ", ".join(payload_failures)
            )
        final_contract = _migration_contract(
            batch_size=batch_size,
            inference_batch_size=inference_batch_size,
            threads=threads,
        )
        if (
            final_contract != migration_contract
            or _contract_digest(final_contract)
            != migration_contract_digest
        ):
            raise RetrievalMigrationError(
                "migration implementation or dependency contract changed "
                "during execution"
            )
        final_metadata = _manifest_metadata(manifest)
        if final_metadata["file_sha256"] != manifest_file_sha:
            raise RetrievalMigrationError(
                "span manifest changed during migration"
            )
        final_model_files, final_model_digest = _verified_model_snapshot(
            model_snapshot,
            expected_digest=observed_model_digest,
        )
        if (
            final_model_digest != observed_model_digest
            or final_model_files != model_files
        ):
            raise RetrievalMigrationError(
                "embedding model snapshot changed during migration"
            )
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "status": "passed",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "manifest": {
                "path": str(manifest),
                "file_sha256": manifest_file_sha,
                "span_manifest_digest": metadata["span_manifest_digest"],
                "profile_digest": metadata["profile_digest"],
                "model_manifest_digest": metadata[
                    "embedding_model_manifest_digest"
                ],
                "spans": expected,
            },
            "qdrant": {
                "url": "loopback",
                "collection": collection,
                "generation": generation,
                "created": created,
                "created_payload_indexes": indexes,
                "points": observed,
                "point_set": point_set,
                "content_payload": False,
                "server_version": client.info().version,
                "client_version": importlib.metadata.version("qdrant-client"),
            },
            "embedding": {
                "model": metadata["embedding_model"],
                "model_snapshot": str(model_snapshot),
                "model_manifest_digest": observed_model_digest,
                "model_files": model_files,
                "fastembed_version": importlib.metadata.version("fastembed"),
                "local_files_only": True,
                "threads": threads,
                "inference_batch_size": inference_batch_size,
                "publication_batch_size": batch_size,
            },
            "migration_contract": migration_contract,
            "migration_contract_digest": migration_contract_digest,
            "checkpoint": {
                "path": str(state_path),
                "last_row_id": last_row_id,
                "points": processed,
                "vector_attestations": attested_points,
                "vector_set_sha256": attested_vector_digest,
            },
            "canonical_mutation": "disabled",
            "prior_generations_modified": False,
        }
        evidence["evidence_digest"] = hashlib.sha256(
            json.dumps(
                evidence,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        security.atomic_write_json(evidence_path, evidence, replace=False)
        return evidence
    finally:
        state.close()
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=artifact_runtime.DEFAULT_DERIVED_ROOT
        / "artifact-spans-p20260721v2.sqlite3",
    )
    parser.add_argument("--model-snapshot", type=Path, required=True)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--inference-batch-size", type=int, default=64)
    parser.add_argument("--threads", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    security.activate_private_umask()
    args = _parser().parse_args(argv)
    runtime = artifact_runtime.load_runtime()
    try:
        result = migrate(
            runtime=runtime,
            manifest=args.manifest,
            model_snapshot=args.model_snapshot,
            collection=args.collection,
            state_path=args.state,
            evidence_path=args.evidence,
            batch_size=args.batch_size,
            inference_batch_size=args.inference_batch_size,
            threads=args.threads,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
