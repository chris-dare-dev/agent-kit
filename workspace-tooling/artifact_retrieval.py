#!/usr/bin/env python3
"""Exact-span hybrid retrieval over one manifest-backed generation."""

from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import platform
import re
import sqlite3
from contextlib import contextmanager, closing
import stat
import struct
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Any, Callable, Iterable, Sequence

import artifact_ingestion as ingestion
import artifact_security as security
import platform_compat


SCHEMA_VERSION = 1
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
QDRANT_SERVER_VERSION = "1.16.3"
RANKING_VERSION = "manifest-rrf-cross-encoder-v1"
EMBEDDING_DIMENSIONS = 384
RRF_K = 60.0
LEXICAL_CANDIDATES = 64
VECTOR_CANDIDATES = 64
UNION_CANDIDATES = 96
RERANK_CANDIDATES = 24
MAX_RESULTS_PER_ARTIFACT = 2
FTS_BM25_WEIGHTS = (8.0, 2.0, 4.0, 1.0)
DUPLICATE_OVERLAP_IOU = 0.80
CROSS_RERANK_WEIGHT = 0.75
FUSION_RERANK_WEIGHT = 0.25
MODEL_THREADS = 4
RERANK_BATCH_SIZE = 24
MAX_CANONICAL_BYTES = 16 * 1024 * 1024
TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "under",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}
IDENTIFIER_LIKE = re.compile(
    r"^(?=.{4,128}$)(?=.*(?:[0-9]|[_.:/@-]))[A-Za-z0-9_.:/@-]+$"
)


class RetrievalError(RuntimeError):
    """The retrieval generation or request is invalid or unverifiable."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ranking_contract() -> dict[str, Any]:
    """Return every explicit ranking/runtime input bound by release evidence."""
    versions: dict[str, str] = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }
    for distribution in (
        "fastembed",
        "numpy",
        "onnxruntime",
        "qdrant-client",
        "tokenizers",
    ):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RetrievalError(
                f"ranking dependency is unavailable: {distribution}"
            ) from exc
    return {
        "ranking_version": RANKING_VERSION,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "rrf_k": RRF_K,
        "lexical_candidates": LEXICAL_CANDIDATES,
        "vector_candidates": VECTOR_CANDIDATES,
        "union_candidates": UNION_CANDIDATES,
        "rerank_candidates": RERANK_CANDIDATES,
        "max_results_per_artifact": MAX_RESULTS_PER_ARTIFACT,
        "fts_bm25_weights": list(FTS_BM25_WEIGHTS),
        "duplicate_overlap_iou": DUPLICATE_OVERLAP_IOU,
        "cross_rerank_weight": CROSS_RERANK_WEIGHT,
        "fusion_rerank_weight": FUSION_RERANK_WEIGHT,
        "model_threads": MODEL_THREADS,
        "rerank_batch_size": RERANK_BATCH_SIZE,
        "implementation_sha256": _file_sha256(Path(__file__).resolve()),
        "runtime_versions": versions,
        "qdrant_server_version": QDRANT_SERVER_VERSION,
    }


@dataclass
class Candidate:
    point_id: str
    span_id: str
    row: dict[str, Any]
    vector_rank: int | None = None
    vector_score: float | None = None
    lexical_rank: int | None = None
    lexical_bm25: float | None = None
    fusion_rank: int | None = None
    cross_rank: int | None = None
    cross_score: float | None = None
    fusion_score: float = 0.0
    final_score: float = 0.0
    alternates: list[dict[str, Any]] | None = None


Reranker = Callable[[str, Sequence[str]], Sequence[float]]


def _safe_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 1000 or "\0" in normalized:
        raise RetrievalError("query must contain 1-1000 safe characters")
    return normalized


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in TERM.finditer(query.casefold()):
        term = match.group(0)
        if term in STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= 64:
            break
    return terms


def _fts_query(query: str) -> str | None:
    terms = _query_terms(query)
    if not terms:
        return None
    escaped = [
        '"' + term.replace('"', '""') + '"'
        for term in terms
    ]
    return " OR ".join(escaped)


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        str(row[0]): json.loads(str(row[1]))
        for row in connection.execute(
            "SELECT key, value_json FROM metadata ORDER BY key"
        )
    }


class LexicalIndex:
    """Read and post-filter the exact span manifest."""

    def __init__(
        self,
        path: Path,
        *,
        generation: str,
        profile_digest: str | None = None,
        model_manifest_digest: str | None = None,
    ):
        self.path = path.expanduser().absolute()
        security.require_private_file(self.path)
        flags = (
            os.O_RDONLY | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self._descriptor = os.open(self.path, flags)
        opened = os.fstat(self._descriptor)
        named = self.path.lstat()
        self._identity = self._file_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not security._owner_and_mode_ok(opened, self.path)
            or self._identity != self._file_identity(named)
        ):
            os.close(self._descriptor)
            self._descriptor = -1
            raise RetrievalError(
                "span manifest changed while its immutable identity was pinned"
            )
        self.generation = generation
        try:
            with self._read() as connection:
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if version != SCHEMA_VERSION:
                    raise RetrievalError(
                        f"unsupported span manifest schema {version}"
                    )
                self.metadata = _metadata(connection)
                integrity = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                required = {
                    "generation",
                    "profile_id",
                    "profile_digest",
                    "embedding_model_manifest_digest",
                    "span_manifest_digest",
                    "spans",
                    "catalog_status",
                    "catalog_artifacts",
                }
                missing = sorted(required - set(self.metadata))
                if missing:
                    raise RetrievalError(
                        f"span manifest metadata is incomplete: {missing}"
                    )
                span_counts = connection.execute(
                    """
                    SELECT
                      COUNT(*),
                      COALESCE(SUM(
                        ready=1
                        AND catalog_current=1
                        AND collection_generation=?
                        AND profile_id=?
                        AND profile_digest=?
                      ), 0)
                    FROM spans
                    """,
                    (
                        self.metadata["generation"],
                        self.metadata["profile_id"],
                        self.metadata["profile_digest"],
                    ),
                ).fetchone()
                artifact_counts = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(catalog_current=1), 0)
                    FROM artifacts
                    """
                ).fetchone()
            if integrity != "ok":
                raise RetrievalError(
                    f"span manifest quick-check failed: {integrity}"
                )
            expected_spans = int(self.metadata["spans"])
            if (
                self.metadata["catalog_status"] != "complete"
                or int(span_counts[0]) != expected_spans
                or int(span_counts[1]) != expected_spans
                or int(artifact_counts[0])
                != int(self.metadata["catalog_artifacts"])
                or int(artifact_counts[1])
                != int(self.metadata["catalog_artifacts"])
            ):
                raise RetrievalError(
                    "span manifest is not one complete, current, uniform generation"
                )
            expected = {
                "generation": generation,
                "profile_digest": profile_digest,
                "embedding_model_manifest_digest": model_manifest_digest,
            }
            for key, value in expected.items():
                if value is not None and self.metadata.get(key) != value:
                    raise RetrievalError(
                        f"span manifest {key} mismatch: "
                        f"{self.metadata.get(key)!r} != {value!r}"
                    )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _file_identity(info: os.stat_result) -> tuple[int, ...]:
        return platform_compat.file_identity(info)

    def _assert_identity(self) -> None:
        if self._descriptor < 0:
            raise RetrievalError("span manifest is closed")
        try:
            security.require_private_file(self.path)
            named = self.path.lstat()
            opened = os.fstat(self._descriptor)
        except (OSError, security.PrivateStateError) as exc:
            raise RetrievalError(
                "span manifest immutable identity is unavailable"
            ) from exc
        if (
            self._file_identity(named) != self._identity
            or self._file_identity(opened) != self._identity
        ):
            raise RetrievalError("span manifest immutable identity changed")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Open the manifest read-only, bound to the descriptor already pinned,
        and CLOSE it on exit.

        A context manager, not a bare Connection: `with sqlite3.connect(...)`
        commits the transaction and leaves the handle open, which on Windows
        pins the file and makes the manifest un-removable by its own owner.
        All three call sites already used `with`, so making the return value a
        real context manager fixes every one of them.

        The threat is a path swapped between the identity check and the read.
        POSIX closes it by handing sqlite `/dev/fd/<fd>`, so the connection reads
        the pinned inode and the name is never resolved again.

        Windows has no `/dev/fd`, and it closes the same hole a different way:
        a file opened without FILE_SHARE_DELETE -- which is what `os.open` gives
        you -- cannot be renamed, replaced or unlinked while the handle is held.
        Verified directly: `os.replace` over it, `os.unlink` and `os.rename` all
        raise PermissionError (WinError 5 / 32 / 32) while `self._descriptor` is
        open. So connecting by path here reaches the same guarantee by a
        different mechanism, and the `_assert_identity()` calls either side of it
        stay in place regardless.

        This is NOT a relaxation: dropping the descriptor would be. Keeping it
        open is what makes the path connect safe.
        """
        self._assert_identity()
        if platform_compat.IS_WINDOWS:
            uri = f"file:{self.path.as_posix()}?mode=ro&immutable=1"
        else:
            uri = f"file:/dev/fd/{self._descriptor}?mode=ro&immutable=1"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        try:
            self._assert_identity()
            yield connection
        finally:
            connection.close()

    def close(self) -> None:
        descriptor = getattr(self, "_descriptor", -1)
        if descriptor >= 0:
            os.close(descriptor)
            self._descriptor = -1

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    @staticmethod
    def _filters(
        *,
        project: str | None,
        artifact_type: str | None,
        authority_class: str | None,
        repository: str | None,
        lifecycle_hint: str | None,
    ) -> tuple[list[str], list[Any]]:
        where = ["s.ready=1", "s.catalog_current=1"]
        parameters: list[Any] = []
        for column, value in (
            ("project", project),
            ("artifact_type", artifact_type),
            ("authority_class", authority_class),
            ("repository", repository),
        ):
            if value is not None:
                where.append(f"s.{column}=?")
                parameters.append(value)
        if lifecycle_hint is not None:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(s.lifecycle_hints_json) "
                "WHERE json_each.value=?)"
            )
            parameters.append(lifecycle_hint)
        return where, parameters

    def search(
        self,
        query: str,
        *,
        limit: int = LEXICAL_CANDIDATES,
        project: str | None = None,
        artifact_type: str | None = None,
        authority_class: str | None = None,
        repository: str | None = None,
        lifecycle_hint: str | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        expression = _fts_query(query)
        if expression is None:
            return []
        where, parameters = self._filters(
            project=project,
            artifact_type=artifact_type,
            authority_class=authority_class,
            repository=repository,
            lifecycle_hint=lifecycle_hint,
        )
        values: list[Any] = [expression, *parameters, limit]
        lexical_weights = ", ".join(str(value) for value in FTS_BM25_WEIGHTS)
        sql = (
            f"""
            SELECT s.*,
                   bm25(spans_fts, {lexical_weights})
                     AS lexical_score
            FROM spans_fts
            JOIN spans AS s ON s.row_id=spans_fts.rowid
            WHERE spans_fts MATCH ? AND
            """
            + " AND ".join(where)
            + " ORDER BY lexical_score ASC, s.span_id ASC LIMIT ?"
        )
        with self._read() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [
            (
                {
                    key: row[key]
                    for key in row.keys()
                    if key != "lexical_score"
                },
                float(row["lexical_score"]),
            )
            for row in rows
        ]

    def lookup(self, point_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        values = sorted(set(point_ids))
        found: dict[str, dict[str, Any]] = {}
        if not values:
            return found
        with self._read() as connection:
            for start in range(0, len(values), 500):
                batch = values[start : start + 500]
                rows = connection.execute(
                    "SELECT * FROM spans WHERE ready=1 AND catalog_current=1 "
                    "AND point_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ).fetchall()
                for row in rows:
                    found[str(row["point_id"])] = dict(row)
        return found


def qdrant_vector_leaf_sha256(
    point_id: str,
    vector: Any,
) -> bytes:
    """Return the canonical point-bound digest used by vector-set proofs."""
    if (
        isinstance(vector, (str, bytes, dict))
        or not isinstance(vector, Sequence)
        or len(vector) != EMBEDDING_DIMENSIONS
    ):
        raise RetrievalError(
            f"Qdrant point vector shape is invalid: {point_id}"
        )
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise RetrievalError(
            f"Qdrant point vector is non-finite: {point_id}"
        )
    try:
        packed = struct.pack(
            f"<{EMBEDDING_DIMENSIONS}f",
            *values,
        )
    except (OverflowError, struct.error) as exc:
        raise RetrievalError(
            f"Qdrant point vector cannot be canonicalized: {point_id}"
        ) from exc
    return hashlib.sha256(
        point_id.encode("utf-8") + b"\0" + packed
    ).digest()


def verify_qdrant_point_set(
    *,
    client: Any,
    collection: str,
    manifest: Path,
    page_size: int = 500,
) -> dict[str, Any]:
    """Prove every Qdrant point identity/payload equals its manifest row."""
    if not 1 <= page_size <= 2_000:
        raise RetrievalError("point verification page size is invalid")
    manifest = manifest.expanduser().absolute()
    security.require_private_file(manifest)
    with closing(sqlite3.connect(
        f"file:{manifest.resolve()}?mode=ro&immutable=1",
        uri=True,
    )) as connection, connection:
        connection.row_factory = sqlite3.Row
        metadata = _metadata(connection)
        expected_count = int(metadata["spans"])
        actual_count = int(
            connection.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        )
        expected_ids = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT point_id
                FROM spans
                WHERE ready=1 AND catalog_current=1
                ORDER BY point_id
                """
            )
        }
        if actual_count != expected_count or len(expected_ids) != expected_count:
            raise RetrievalError(
                "span manifest count/currentness differs from its metadata"
            )
        observed_count = 0
        observed_ids: set[str] = set()
        vector_leaves: list[bytes] = []
        offset: Any | None = None
        offsets: set[str] = set()
        while True:
            points, next_offset = client.scroll(
                collection_name=collection,
                offset=offset,
                limit=page_size,
                with_payload=True,
                with_vectors=True,
            )
            point_ids = [str(point.id) for point in points]
            page_ids = set(point_ids)
            if (
                len(point_ids) != len(page_ids)
                or observed_ids.intersection(page_ids)
            ):
                raise RetrievalError("Qdrant point scan repeated an identity")
            observed_ids.update(page_ids)
            rows: dict[str, sqlite3.Row] = {}
            for start in range(0, len(point_ids), 500):
                batch = point_ids[start : start + 500]
                if not batch:
                    continue
                found = connection.execute(
                    "SELECT * FROM spans WHERE ready=1 AND catalog_current=1 "
                    "AND point_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                )
                rows.update({str(row["point_id"]): row for row in found})
            if len(rows) != len(point_ids):
                raise RetrievalError(
                    "Qdrant contains a point absent from the exact manifest"
                )
            for point in points:
                point_id = str(point.id)
                row = rows[point_id]
                expected_payload = {
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
                if not isinstance(point.payload, dict) or point.payload != expected_payload:
                    raise RetrievalError(
                        f"Qdrant point payload differs from manifest: {point_id}"
                    )
                vector_leaves.append(
                    qdrant_vector_leaf_sha256(
                        point_id,
                        getattr(point, "vector", None),
                    )
                )
            observed_count += len(points)
            if next_offset is None:
                break
            marker = repr(next_offset)
            if marker in offsets:
                raise RetrievalError("Qdrant point scan offset repeated")
            offsets.add(marker)
            offset = next_offset
        if observed_count != expected_count or observed_ids != expected_ids:
            raise RetrievalError(
                "Qdrant point identity set does not match the manifest: "
                f"{observed_count} != {expected_count}"
            )
        vector_digest = hashlib.sha256()
        for leaf in sorted(vector_leaves):
            vector_digest.update(leaf)
    return {
        "points": observed_count,
        "span_manifest_digest": str(metadata["span_manifest_digest"]),
        "payload_contract": "exact-manifest-row-v1",
        "vector_contract": (
            f"sorted-point-id-sha256-float32le-v1/{EMBEDDING_DIMENSIONS}"
        ),
        "vector_set_sha256": vector_digest.hexdigest(),
    }


def _qdrant_filter(
    models: Any,
    *,
    generation: str,
    profile_digest: str,
    project: str | None,
    artifact_type: str | None,
    authority_class: str | None,
    repository: str | None,
    lifecycle_hint: str | None,
) -> Any:
    must = [
        models.FieldCondition(
            key="catalog_current",
            match=models.MatchValue(value=True),
        ),
        models.FieldCondition(
            key="ready",
            match=models.MatchValue(value=True),
        ),
        models.FieldCondition(
            key="collection_generation",
            match=models.MatchValue(value=generation),
        ),
        models.FieldCondition(
            key="profile_digest",
            match=models.MatchValue(value=profile_digest),
        ),
    ]
    for key, value in (
        ("project", project),
        ("artifact_type", artifact_type),
        ("authority_class", authority_class),
        ("repository", repository),
        ("lifecycle_hints", lifecycle_hint),
    ):
        if value is not None:
            must.append(
                models.FieldCondition(
                    key=key,
                    match=models.MatchValue(value=value),
                )
            )
    return models.Filter(must=must)


def _safe_source(workspace: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RetrievalError("result has an unsafe canonical path")
    workspace = workspace.resolve(strict=True)
    path = Path(os.path.abspath(os.fspath(workspace / relative)))
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise RetrievalError("result escapes the canonical workspace") from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RetrievalError("canonical path contains a symlink")
    return path


def _verified_canonical_span(
    *,
    workspace: Path,
    artifact: ingestion.CatalogArtifact,
    row: dict[str, Any],
) -> dict[str, Any]:
    provenance = {
        "artifact_id": artifact.artifact_id,
        "revision_id": artifact.revision_id,
        "relative_path": artifact.relative_path,
        "content_sha256": artifact.content_sha256,
    }
    if any(
        not hmac.compare_digest(str(row.get(key, "")), str(value))
        for key, value in provenance.items()
    ):
        raise RetrievalError(
            "manifest row provenance does not match the current catalog artifact"
        )
    if artifact.byte_size > MAX_CANONICAL_BYTES:
        raise RetrievalError("canonical source exceeds the retrieval size limit")
    path = _safe_source(workspace, artifact.relative_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    data = bytearray()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RetrievalError("canonical source is not a regular file")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            data.extend(block)
        after = os.fstat(handle.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise RetrievalError("canonical source changed during verification")
    if (
        digest.hexdigest() != artifact.content_sha256
        or after.st_size != artifact.byte_size
        or after.st_mtime_ns != artifact.mtime_ns
    ):
        raise RetrievalError("canonical source no longer matches the catalog")
    start = int(row["byte_start"])
    end = int(row["byte_end"])
    raw = bytes(data)
    if not 0 <= start < end <= len(raw):
        raise RetrievalError("stored canonical interval is out of bounds")
    span = raw[start:end]
    span_sha = hashlib.sha256(span).hexdigest()
    if not hmac.compare_digest(span_sha, str(row["span_sha256"])):
        raise RetrievalError("canonical span hash does not match the manifest")
    try:
        content = span.decode("utf-8", errors="strict")
        char_start = len(raw[:start].decode("utf-8", errors="strict"))
        char_end = char_start + len(content)
    except UnicodeDecodeError as exc:
        raise RetrievalError("canonical interval is not valid UTF-8") from exc
    line_start = raw[:start].count(b"\n") + 1
    line_end = raw[: max(start, end - 1)].count(b"\n") + 1
    expected = (
        int(row["char_start"]),
        int(row["char_end"]),
        int(row["line_start"]),
        int(row["line_end"]),
    )
    observed = (char_start, char_end, line_start, line_end)
    if observed != expected:
        raise RetrievalError("canonical coordinates do not match the manifest")
    return {
        "content": content,
        "span_sha256": span_sha,
        "span": {
            "char_start": char_start,
            "char_end": char_end,
            "byte_start": start,
            "byte_end": end,
            "line_start": line_start,
            "line_end": line_end,
            "byte_interval": "half-open",
            "line_interval": "one-based-inclusive",
        },
        "source_sha256_verified": True,
        "span_sha256_verified": True,
    }


def _same_copy(left: Candidate, right: Candidate) -> bool:
    fields = ("content_sha256", "byte_start", "byte_end", "span_sha256")
    return all(left.row.get(name) == right.row.get(name) for name in fields)


def _overlap_duplicate(left: Candidate, right: Candidate) -> bool:
    if left.row.get("revision_id") != right.row.get("revision_id"):
        return False
    left_start, left_end = int(left.row["byte_start"]), int(left.row["byte_end"])
    right_start, right_end = int(right.row["byte_start"]), int(right.row["byte_end"])
    intersection = max(0, min(left_end, right_end) - max(left_start, right_start))
    union = max(left_end, right_end) - min(left_start, right_start)
    return bool(union and intersection / union >= DUPLICATE_OVERLAP_IOU)


def _abstention_features(
    query: str,
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    if not candidates:
        return {
            "has_candidate": False,
            "exact_identifier": False,
            "channel_agreement": False,
            "vector_score": None,
            "lexical_bm25": None,
            "cross_score": None,
            "cross_margin": None,
        }
    top = candidates[0]
    terms = _query_terms(query)
    identifiers = set(str(top.row.get("identifiers") or "").split())
    exact_identifier = any(
        IDENTIFIER_LIKE.fullmatch(term) and term in identifiers
        for term in terms
    )
    cross_margin = None
    if top.cross_score is not None and len(candidates) > 1:
        second = candidates[1].cross_score
        if second is not None:
            cross_margin = top.cross_score - second
    return {
        "has_candidate": True,
        "exact_identifier": exact_identifier,
        "channel_agreement": (
            top.vector_rank is not None and top.lexical_rank is not None
        ),
        "vector_score": top.vector_score,
        "lexical_bm25": top.lexical_bm25,
        "cross_score": top.cross_score,
        "cross_margin": cross_margin,
    }


def should_abstain(
    features: dict[str, Any],
    policy: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    if not features.get("has_candidate"):
        return True, "no-candidate"
    if policy is None:
        return False, None
    exact_min = float(policy["exact_identifier_cross_min"])
    agreement_min = float(policy["agreement_cross_min"])
    single_min = float(policy["single_channel_cross_min"])
    margin_min = float(policy["cross_margin_min"])
    cross = features.get("cross_score")
    margin = features.get("cross_margin")
    if cross is None:
        return True, "reranker-unavailable"
    if features.get("exact_identifier") and cross >= exact_min:
        return False, None
    if (
        features.get("channel_agreement")
        and cross >= agreement_min
        and margin is not None
        and margin >= margin_min
    ):
        return False, None
    if cross >= single_min and margin is not None and margin >= margin_min:
        return False, None
    return True, "below-calibrated-threshold"


class HybridRetriever:
    def __init__(
        self,
        *,
        workspace: Path,
        catalog: Path,
        lexical: LexicalIndex,
    ):
        self.workspace = workspace.resolve(strict=True)
        self.catalog = catalog
        self.lexical = lexical
        self.catalog_lock = threading.Lock()
        self.catalog_mtime_ns = -1
        self.artifacts: dict[str, ingestion.CatalogArtifact] = {}

    def current_artifacts(self) -> dict[str, ingestion.CatalogArtifact]:
        mtime = self.catalog.stat().st_mtime_ns
        if mtime == self.catalog_mtime_ns:
            return self.artifacts
        with self.catalog_lock:
            mtime = self.catalog.stat().st_mtime_ns
            if mtime != self.catalog_mtime_ns:
                _run, artifacts = ingestion.load_current_artifacts(self.catalog)
                self.artifacts = {
                    artifact.relative_path: artifact for artifact in artifacts
                }
                self.catalog_mtime_ns = mtime
        return self.artifacts

    def search(
        self,
        *,
        client: Any,
        collection: str,
        query: str,
        query_vector: Sequence[float],
        limit: int,
        mode: str,
        reranker: Reranker | None,
        abstention_policy: dict[str, Any] | None = None,
        project: str | None = None,
        artifact_type: str | None = None,
        authority_class: str | None = None,
        repository: str | None = None,
        lifecycle_hint: str | None = None,
    ) -> dict[str, Any]:
        query = _safe_query(query)
        if mode not in ("vector", "lexical", "hybrid", "hybrid-rerank"):
            raise RetrievalError(f"unsupported retrieval mode: {mode}")
        if not 1 <= limit <= 25:
            raise RetrievalError("limit must be between 1 and 25")
        generation = str(self.lexical.metadata["generation"])
        profile_digest = str(self.lexical.metadata["profile_digest"])
        candidates: dict[str, Candidate] = {}

        if mode in ("vector", "hybrid", "hybrid-rerank"):
            from qdrant_client import models

            response = client.query_points(
                collection_name=collection,
                query=list(query_vector),
                query_filter=_qdrant_filter(
                    models,
                    generation=generation,
                    profile_digest=profile_digest,
                    project=project,
                    artifact_type=artifact_type,
                    authority_class=authority_class,
                    repository=repository,
                    lifecycle_hint=lifecycle_hint,
                ),
                limit=VECTOR_CANDIDATES,
                with_payload=False,
            )
            vector_points: list[tuple[Any, float]] = []
            for point in response.points:
                score = float(point.score)
                if not math.isfinite(score):
                    raise RetrievalError("vector search returned a non-finite score")
                vector_points.append((point, score))
            vector_points.sort(
                key=lambda item: (-item[1], str(item[0].id))
            )
            rows = self.lexical.lookup(
                str(point.id) for point, _score in vector_points
            )
            for rank, (point, score) in enumerate(vector_points, 1):
                point_id = str(point.id)
                row = rows.get(point_id)
                if row is None:
                    continue
                candidate = Candidate(
                    point_id=point_id,
                    span_id=str(row["span_id"]),
                    row=row,
                    vector_rank=rank,
                    vector_score=score,
                )
                candidates[candidate.span_id] = candidate

        if mode in ("lexical", "hybrid", "hybrid-rerank"):
            rows = self.lexical.search(
                query,
                limit=LEXICAL_CANDIDATES,
                project=project,
                artifact_type=artifact_type,
                authority_class=authority_class,
                repository=repository,
                lifecycle_hint=lifecycle_hint,
            )
            for rank, (row, bm25) in enumerate(rows, 1):
                if not math.isfinite(float(bm25)):
                    raise RetrievalError("lexical search returned a non-finite score")
                span_id = str(row["span_id"])
                candidate = candidates.get(span_id)
                if candidate is None:
                    candidate = Candidate(
                        point_id=str(row["point_id"]),
                        span_id=span_id,
                        row=row,
                    )
                    candidates[span_id] = candidate
                candidate.lexical_rank = rank
                candidate.lexical_bm25 = bm25

        for candidate in candidates.values():
            if mode == "vector":
                candidate.fusion_score = float(candidate.vector_score or 0.0)
            elif mode == "lexical":
                candidate.fusion_score = -float(candidate.lexical_bm25 or 0.0)
            else:
                if candidate.vector_rank is not None:
                    candidate.fusion_score += 1.0 / (
                        RRF_K + candidate.vector_rank
                    )
                if candidate.lexical_rank is not None:
                    candidate.fusion_score += 1.0 / (
                        RRF_K + candidate.lexical_rank
                    )
            candidate.final_score = candidate.fusion_score

        if mode == "vector":
            ranked = sorted(
                candidates.values(),
                key=lambda value: (
                    value.vector_rank if value.vector_rank is not None else 10**9,
                    value.span_id,
                ),
            )
        elif mode == "lexical":
            ranked = sorted(
                candidates.values(),
                key=lambda value: (
                    value.lexical_rank if value.lexical_rank is not None else 10**9,
                    value.span_id,
                ),
            )
        else:
            ranked = sorted(
                candidates.values(),
                key=lambda value: (-value.fusion_score, value.span_id),
            )
        ranked = ranked[:UNION_CANDIDATES]
        for index, candidate in enumerate(ranked, 1):
            candidate.fusion_rank = index

        collapsed: list[Candidate] = []
        for candidate in ranked:
            duplicate = next(
                (
                    existing
                    for existing in collapsed
                    if _same_copy(existing, candidate)
                    or _overlap_duplicate(existing, candidate)
                ),
                None,
            )
            if duplicate is None:
                candidate.alternates = []
                collapsed.append(candidate)
            else:
                duplicate.alternates.append(
                    {
                        "span_id": candidate.span_id,
                        "revision_id": candidate.row["revision_id"],
                        "relative_path": candidate.row["relative_path"],
                        "byte_start": candidate.row["byte_start"],
                        "byte_end": candidate.row["byte_end"],
                    }
                )

        if mode == "hybrid-rerank":
            if reranker is None:
                raise RetrievalError("hybrid-rerank requires the pinned local model")
            rerank_set = collapsed[:RERANK_CANDIDATES]
            documents = [
                (
                    str(candidate.row.get("relative_path") or "")
                    + "\n"
                    + str(candidate.row.get("heading") or "")
                    + "\n"
                    + str(candidate.row.get("content") or "")
                )
                for candidate in rerank_set
            ]
            scores = [float(value) for value in reranker(query, documents)]
            if len(scores) != len(rerank_set):
                raise RetrievalError("reranker returned the wrong score count")
            if not all(math.isfinite(value) for value in scores):
                raise RetrievalError("reranker returned a non-finite score")
            cross_order = sorted(
                range(len(rerank_set)),
                key=lambda index: (-scores[index], rerank_set[index].span_id),
            )
            cross_ranks = {
                candidate_index: rank
                for rank, candidate_index in enumerate(cross_order, 1)
            }
            for index, (candidate, score) in enumerate(zip(rerank_set, scores)):
                candidate.cross_score = score
                candidate.cross_rank = cross_ranks[index]
                candidate.final_score = (
                    CROSS_RERANK_WEIGHT / (RRF_K + candidate.cross_rank)
                    + FUSION_RERANK_WEIGHT
                    / (RRF_K + int(candidate.fusion_rank or 10**9))
                )
            collapsed = sorted(
                rerank_set,
                key=lambda value: (
                    -value.final_score,
                    -value.fusion_score,
                    value.span_id,
                ),
            ) + collapsed[RERANK_CANDIDATES:]

        verified_candidates: list[Candidate] = []
        verified_results: list[dict[str, Any]] = []
        verification_failures: list[dict[str, str]] = []
        per_artifact: dict[str, int] = {}
        verification_target = max(
            limit,
            2 if abstention_policy is not None else limit,
        )
        if collapsed:
            artifacts = self.current_artifacts()
            for candidate in collapsed:
                row = candidate.row
                relative = str(row["relative_path"])
                artifact = artifacts.get(relative)
                if (
                    artifact is None
                    or artifact.revision_id != str(row["revision_id"])
                ):
                    verification_failures.append(
                        {"span_id": candidate.span_id, "reason": "not-current"}
                    )
                    continue
                logical_id = str(row["artifact_id"])
                if per_artifact.get(logical_id, 0) >= MAX_RESULTS_PER_ARTIFACT:
                    continue
                try:
                    verified = _verified_canonical_span(
                        workspace=self.workspace,
                        artifact=artifact,
                        row=row,
                    )
                except (OSError, RetrievalError) as exc:
                    verification_failures.append(
                        {
                            "span_id": candidate.span_id,
                            "reason": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                    continue
                per_artifact[logical_id] = per_artifact.get(logical_id, 0) + 1
                verified_candidates.append(candidate)
                verified_results.append(
                    {
                        "point_id": candidate.point_id,
                        "span_id": candidate.span_id,
                        "logical_artifact_id": logical_id,
                        "revision_id": row["revision_id"],
                        "relative_path": relative,
                        "heading": row["heading"],
                        "artifact_type": row["artifact_type"],
                        "authority_class": row["authority_class"],
                        "project": row["project"],
                        "repository": row["repository"],
                        "content_sha256": row["content_sha256"],
                        "span_sha256": verified["span_sha256"],
                        "content": verified["content"],
                        "span": verified["span"],
                        "source_sha256_verified": True,
                        "span_sha256_verified": True,
                        "untrusted_content": True,
                        "alternate_provenance": candidate.alternates,
                        "ranking": {
                            "version": RANKING_VERSION,
                            "vector_rank": candidate.vector_rank,
                            "vector_score": candidate.vector_score,
                            "lexical_rank": candidate.lexical_rank,
                            "lexical_bm25": candidate.lexical_bm25,
                            "fusion_rank": candidate.fusion_rank,
                            "fusion_score": candidate.fusion_score,
                            "cross_encoder_rank": candidate.cross_rank,
                            "cross_encoder_score": candidate.cross_score,
                            "final_score": candidate.final_score,
                        },
                    }
                )
                if len(verified_results) >= verification_target:
                    break
        features = _abstention_features(query, verified_candidates)
        abstained, abstention_reason = should_abstain(
            features,
            abstention_policy,
        )
        if not verified_results and verification_failures:
            abstention_reason = "no-verified-result"
        results = [] if abstained else verified_results[:limit]
        return {
            "query": query,
            "mode": mode,
            "generation": generation,
            "profile_digest": profile_digest,
            "current_only": True,
            "abstained": abstained,
            "abstention_reason": abstention_reason,
            "abstention_features": features,
            "candidate_counts": {
                "union": len(candidates),
                "bounded_union": len(ranked),
                "collapsed": len(collapsed),
                "verified": len(verified_candidates),
            },
            "verification_failures": verification_failures[:20],
            "results": results,
        }
