#!/usr/bin/env python3
"""Resident, UDS-only, read-only policy boundary for artifact memory.

The service keeps retrieval models resident and owns the Qdrant read
credential.  Its owner-private Unix-domain socket exposes only status and
read operations to local stdio adapters; there is no TCP or bearer-token
fallback.  Derived-index mutation remains an out-of-process operator action
with a separate Qdrant admin credential.  The listener rejects
browser-originated requests, oversized bodies, historical snippets, and
unapproved Graphiti pilot access.
"""

from __future__ import annotations

import argparse
import collections
import errno
import hashlib
import json
import os
import queue
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Sequence

import artifact_ingestion as ingestion
import artifact_memory
import artifact_retrieval
import artifact_runtime
import platform_compat
import artifact_security as security


SCHEMA_VERSION = 1

# Wire-protocol major version. Adapters send it on every request as
# PROTOCOL_HEADER; the service echoes its own on every response. Bump this
# ONLY for a breaking request/response shape change -- a long-lived provider
# MCP process keeps its already-loaded adapter code until the session is
# restarted, so the bump is what turns a silent mixed-version conversation
# into an explicit "restart this session" error on both ends.
PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-Artifact-Memory-Protocol"
SCHEMA_HEADER = "X-Artifact-Memory-Schema"
BUILD_HEADER = "X-Artifact-Memory-Build"

# Rollout gate. False accepts version-less requests so the current adapter and
# artifact_service_client keep working while both sides ship; flip to True once
# every local client sends PROTOCOL_HEADER, which closes the stale-client hole
# for good (an adapter too old to send the header is then fenced, not served).
REQUIRE_CLIENT_PROTOCOL = False

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 12
PUBLIC_ROUTES = frozenset(("/v1/status", "/v1/search", "/v1/get", "/v1/facts"))
DEFAULT_HEALTH = (
    artifact_runtime.DEFAULT_DERIVED_ROOT / "artifact-memory-service-health.json"
)


def _service_build() -> str:
    """Short digest of this source file, identifying the running build.

    The substrate is unversioned, so a source digest is the only stable build
    identity available; it lets a client report exactly which service it was
    talking to when a version mismatch was surfaced. Never fatal -- an
    unreadable source file degrades to "unknown" rather than taking the
    listener down.
    """
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:
        return "unknown"


SERVICE_BUILD = _service_build()


class ServiceError(RuntimeError):
    """A bounded service operation could not be completed."""


class ProtocolVersionError(ServiceError):
    """The client speaks a wire protocol this service cannot serve."""


def parse_protocol_version(raw: str | None) -> int | None:
    """Parse a client PROTOCOL_HEADER value, or None when absent.

    Anything that is not a bare non-negative decimal integer is a malformed
    header, not an old client, so it raises rather than degrading to None --
    a garbled version must fail loudly instead of being served as version-less.
    The digit test is ASCII-only on purpose: str.isdigit() also accepts
    non-ASCII forms, where "١" would silently parse as 1 and "²"
    would raise inside int() -- both are misparses, not protocol versions.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate.isascii() or not candidate.isdigit() or len(candidate) > 9:
        raise ProtocolVersionError(
            f"{PROTOCOL_HEADER} must be a decimal protocol major version; this "
            f"client does not speak protocol {PROTOCOL_VERSION} -- restart the "
            "session to load the current adapter"
        )
    return int(candidate)


def require_supported_protocol(raw: str | None) -> None:
    """Fence a client whose wire protocol this service cannot serve."""
    version = parse_protocol_version(raw)
    if version is None:
        if REQUIRE_CLIENT_PROTOCOL:
            raise ProtocolVersionError(
                f"{PROTOCOL_HEADER} is required; this client predates protocol "
                f"{PROTOCOL_VERSION} -- restart the session to load the current adapter"
            )
        return
    if version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"client protocol {version} is not served by this build; the artifact "
            f"memory service speaks protocol {PROTOCOL_VERSION} -- restart the "
            "session to load the current adapter"
        )


class _EmbeddingRequest:
    def __init__(self, text: str):
        self.text = text
        self.event = threading.Event()
        self.vector: Any | None = None
        self.error: BaseException | None = None


class ResidentEmbedder:
    """Micro-batch query inference around one persistent model instance."""

    def __init__(
        self,
        model: Any,
        maximum_batch: int = 32,
        *,
        query_mode: bool = False,
    ):
        self.model = model
        self.maximum_batch = maximum_batch
        self.query_mode = query_mode
        self.model_lock = threading.Lock()
        self.requests: queue.Queue[_EmbeddingRequest | None] = queue.Queue(
            maxsize=MAX_CONCURRENT_REQUESTS * 4
        )
        self.worker = threading.Thread(
            target=self._run,
            name="artifact-embedding-batcher",
            daemon=True,
        )
        self.worker.start()

    def embed(self, documents: Sequence[str]) -> list[Any]:
        with self.model_lock:
            method = self.model.query_embed if self.query_mode else self.model.embed
            return list(method(documents))

    def embed_query(self, text: str, timeout: float = 30.0) -> Any:
        request = _EmbeddingRequest(text)
        try:
            self.requests.put(request, timeout=1.0)
        except queue.Full as exc:
            raise ServiceError("embedding queue is full") from exc
        if not request.event.wait(timeout):
            raise ServiceError("embedding request timed out")
        if request.error is not None:
            raise ServiceError(
                f"embedding inference failed: {type(request.error).__name__}"
            ) from request.error
        return request.vector

    def _run(self) -> None:
        while True:
            first = self.requests.get()
            if first is None:
                return
            batch = [first]
            deadline = time.monotonic() + 0.008
            while len(batch) < self.maximum_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    candidate = self.requests.get(timeout=remaining)
                except queue.Empty:
                    break
                if candidate is None:
                    self.requests.put(None)
                    break
                batch.append(candidate)
            try:
                vectors = self.embed([request.text for request in batch])
                if len(vectors) != len(batch):
                    raise ServiceError("embedding model returned the wrong batch size")
                for request, vector in zip(batch, vectors):
                    request.vector = vector
            except BaseException as exc:
                for request in batch:
                    request.error = exc
            finally:
                for request in batch:
                    request.event.set()

    def close(self) -> None:
        try:
            self.requests.put_nowait(None)
        except queue.Full:
            pass

    def metrics(self) -> dict[str, Any]:
        return {
            "depth": self.requests.qsize(),
            "capacity": self.requests.maxsize,
            "worker_alive": self.worker.is_alive(),
        }


class ResidentReranker:
    """Serialize one pinned local cross-encoder across bounded requests."""

    def __init__(self, model: Any):
        self.model = model
        self.lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.pending = 0
        self.maximum_pending = 0
        self.calls = 0
        self.wait_ms: collections.deque[float] = collections.deque(maxlen=2000)

    def __call__(self, query: str, documents: Sequence[str]) -> Sequence[float]:
        queued = time.perf_counter()
        with self.metrics_lock:
            self.pending += 1
            self.maximum_pending = max(self.maximum_pending, self.pending)
        try:
            with self.lock:
                wait_ms = (time.perf_counter() - queued) * 1000
                with self.metrics_lock:
                    self.calls += 1
                    self.wait_ms.append(wait_ms)
                return list(
                    self.model.rerank(
                        query,
                        documents,
                        batch_size=artifact_retrieval.RERANK_BATCH_SIZE,
                    )
                )
        finally:
            with self.metrics_lock:
                self.pending -= 1

    def metrics(self) -> dict[str, Any]:
        with self.metrics_lock:
            waits = collections.deque(self.wait_ms)
            return {
                "pending": self.pending,
                "maximum_pending": self.maximum_pending,
                "calls": self.calls,
                "p95_wait_ms": ServiceState._percentile(waits, 0.95),
            }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _path_identity(path: Path) -> tuple[int, ...]:
    return platform_compat.file_identity(path.stat())


def _catalog_binding(path: Path) -> dict[str, Any]:
    run_id, artifacts = ingestion.load_current_artifacts(path)
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda value: value.relative_path):
        digest.update(
            (
                artifact.relative_path
                + "\0"
                + artifact.revision_id
                + "\0"
                + artifact.content_sha256
                + "\n"
            ).encode("utf-8")
        )
    return {
        "catalog_run_id": run_id,
        "catalog_artifacts": len(artifacts),
        "catalog_revision_set_sha256": digest.hexdigest(),
    }


def _model_manifest_digest(path: Path) -> str:
    try:
        digest, _files = security.private_tree_manifest(path)
    except security.PrivateStateError as exc:
        raise ServiceError(f"model snapshot is unsafe: {exc}") from exc
    return digest


def _verified_json_evidence(path: Path, expected_digest: str) -> dict[str, Any]:
    security.require_private_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"retrieval evidence is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ServiceError("retrieval evidence must be an object")
    embedded = payload.get("evidence_digest")
    body = dict(payload)
    body.pop("evidence_digest", None)
    observed = _canonical_digest(body)
    if embedded != observed or observed != expected_digest:
        raise ServiceError("retrieval evidence digest does not match")
    if payload.get("status") != "passed":
        raise ServiceError("retrieval evidence did not pass its release gate")
    return payload


class ExactRetrievalState:
    """Frozen matched manifest/Qdrant/models/policy tuple for shadow serving."""

    def __init__(
        self,
        *,
        runtime: artifact_runtime.ArtifactRuntime,
        read_client: Any,
    ):
        config = runtime.retrieval
        if config is None:
            raise ServiceError("exact retrieval configuration is absent")
        if runtime.active_backend != "server":
            raise ServiceError("exact retrieval requires the loopback Qdrant server")
        security.require_private_file(runtime.catalog)
        security.require_private_file(config.manifest)
        if _file_sha256(config.manifest) != config.manifest_sha256:
            raise ServiceError("exact retrieval manifest SHA-256 does not match")
        lexical = artifact_retrieval.LexicalIndex(
            config.manifest,
            generation=config.generation,
            profile_digest=config.profile_digest,
            model_manifest_digest=config.embedding_model_manifest_digest,
        )
        if (
            lexical.metadata.get("span_manifest_digest")
            != config.span_manifest_digest
        ):
            raise ServiceError("exact retrieval span-manifest digest does not match")
        manifest_catalog = {
            "catalog_run_id": lexical.metadata.get("catalog_run_id"),
            "catalog_artifacts": lexical.metadata.get("catalog_artifacts"),
            "catalog_revision_set_sha256": lexical.metadata.get(
                "catalog_revision_set_sha256"
            ),
        }
        if (
            lexical.metadata.get("catalog_status") != "complete"
            or isinstance(manifest_catalog["catalog_run_id"], bool)
            or not isinstance(manifest_catalog["catalog_run_id"], int)
            or isinstance(manifest_catalog["catalog_artifacts"], bool)
            or not isinstance(manifest_catalog["catalog_artifacts"], int)
            or not isinstance(
                manifest_catalog["catalog_revision_set_sha256"], str
            )
            or not artifact_runtime.DIGEST.fullmatch(
                manifest_catalog["catalog_revision_set_sha256"]
            )
        ):
            raise ServiceError(
                "exact retrieval manifest lacks a complete catalog binding"
            )
        current_catalog = _catalog_binding(runtime.catalog)
        if current_catalog != manifest_catalog:
            raise ServiceError(
                "exact retrieval catalog revision set does not match the manifest"
            )
        if (
            lexical.metadata.get("embedding_model")
            != config.embedding_model
            or config.embedding_model != artifact_retrieval.EMBEDDING_MODEL
            or config.reranker_model != artifact_retrieval.RERANKER_MODEL
        ):
            raise ServiceError("exact retrieval model identity does not match")
        if config.ranking_version != artifact_retrieval.RANKING_VERSION:
            raise ServiceError("exact retrieval ranking implementation does not match")
        if (
            _model_manifest_digest(config.embedding_model_snapshot)
            != config.embedding_model_manifest_digest
            or _model_manifest_digest(config.reranker_model_snapshot)
            != config.reranker_model_manifest_digest
        ):
            raise ServiceError("exact retrieval model snapshot digest does not match")
        try:
            policy = json.loads(config.policy_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(f"exact retrieval policy is invalid: {exc}") from exc
        if (
            not isinstance(policy, dict)
            or policy.get("ranking_version") != config.ranking_version
            or policy.get("policy_digest") != config.policy_digest
            or _canonical_digest(policy.get("policy"))
            != config.policy_digest
        ):
            raise ServiceError("exact retrieval policy digest or version does not match")
        system = policy.get("system")
        if (
            not isinstance(system, dict)
            or _canonical_digest(system) != policy.get("system_digest")
            or system.get("generation") != config.generation
            or system.get("collection") != config.collection
            or system.get("span_manifest_digest") != config.span_manifest_digest
            or system.get("span_manifest_file_sha256") != config.manifest_sha256
            or system.get("profile_digest") != config.profile_digest
            or system.get("embedding_model_manifest_digest")
            != config.embedding_model_manifest_digest
            or system.get("reranker_model_manifest_digest")
            != config.reranker_model_manifest_digest
            or system.get("embedding_model") != config.embedding_model
            or system.get("reranker_model") != config.reranker_model
            or system.get("ranking_version") != config.ranking_version
            or system.get("ranking_contract")
            != artifact_retrieval.ranking_contract()
            or system.get("catalog_binding") != manifest_catalog
        ):
            raise ServiceError("exact retrieval frozen system identity does not match")
        try:
            import artifact_retrieval_eval as retrieval_eval
        except ImportError as exc:
            raise ServiceError(
                "exact retrieval release validator is unavailable"
            ) from exc
        if (
            system.get("material_sources")
            != retrieval_eval._material_source_manifest()
            or system.get("evaluation_runtime_contract")
            != retrieval_eval._evaluation_runtime_contract()
            or system.get("retrieval_scope")
            != {
                "name": retrieval_eval.CURRENT_ONLY_SCOPE,
                "filters": dict(retrieval_eval.CURRENT_ONLY_FILTERS),
            }
        ):
            raise ServiceError(
                "exact retrieval release source/runtime contract does not match"
            )
        migration_binding = system.get("migration_evidence")
        if (
            not isinstance(migration_binding, dict)
            or set(migration_binding)
            != {
                "path",
                "file_sha256",
                "evidence_digest",
                "vector_set_sha256",
                "migration_contract_digest",
            }
            or not isinstance(migration_binding.get("path"), str)
        ):
            raise ServiceError(
                "exact retrieval migration evidence binding is invalid"
            )
        migration_path = Path(migration_binding["path"])
        if (
            not migration_path.is_absolute()
            or _file_sha256(
                security.require_private_file(migration_path)
            )
            != migration_binding.get("file_sha256")
        ):
            raise ServiceError(
                "exact retrieval migration evidence file does not match"
            )
        try:
            migration_path.relative_to(
                security.require_private_directory(
                    artifact_runtime.DEFAULT_DERIVED_ROOT
                )
            )
        except ValueError as exc:
            raise ServiceError(
                "exact retrieval migration evidence is outside derived state"
            ) from exc
        migration = _verified_json_evidence(
            migration_path,
            str(migration_binding.get("evidence_digest", "")),
        )
        development = _verified_json_evidence(
            config.development_evidence,
            config.development_evidence_digest,
        )
        holdout = _verified_json_evidence(
            config.holdout_evidence,
            config.holdout_evidence_digest,
        )
        for split, evidence in (("dev", development), ("holdout", holdout)):
            if (
                evidence.get("split") != split
                or evidence.get("system_digest") != policy.get("system_digest")
                or evidence.get("policy", {}).get("policy_digest")
                != config.policy_digest
                or evidence.get("gate", {}).get("status") != "passed"
            ):
                raise ServiceError(
                    f"exact retrieval {split} evidence identity does not match"
                )
        development_binding = policy.get("development_evidence")
        expected_development_policy = {
            key: value
            for key, value in policy.items()
            if key != "development_evidence"
        }
        if (
            policy.get("development_selection_feasible") is not True
            or not isinstance(development_binding, dict)
            or development_binding.get("path")
            != str(config.development_evidence)
            or development_binding.get("evidence_digest")
            != config.development_evidence_digest
            or development_binding.get("file_sha256")
            != _file_sha256(config.development_evidence)
            or development.get("policy") != expected_development_policy
            or holdout.get("policy") != policy
        ):
            raise ServiceError(
                "exact retrieval policy/evidence chain does not match"
            )
        gold_split_files = system.get("gold_split_file_sha256")
        holdout_gold = holdout.get("gold")
        if (
            not isinstance(gold_split_files, dict)
            or set(gold_split_files) != {"dev", "holdout"}
            or any(
                not isinstance(value, str)
                or not artifact_runtime.DIGEST.fullmatch(value)
                for value in gold_split_files.values()
            )
            or not isinstance(holdout_gold, dict)
            or not isinstance(holdout_gold.get("digest"), str)
            or not artifact_runtime.DIGEST.fullmatch(
                holdout_gold["digest"]
            )
        ):
            raise ServiceError(
                "exact retrieval gold release identity does not match"
            )
        try:
            retrieval_eval.validate_completed_holdout_ledger(
                gold_file_sha256=gold_split_files["holdout"],
                generation=config.generation,
                span_manifest_digest=config.span_manifest_digest,
                policy_digest=config.policy_digest,
                system_digest=str(policy["system_digest"]),
                gold_digest=holdout_gold["digest"],
                evidence_digest=config.holdout_evidence_digest,
                evidence_file_sha256=_file_sha256(
                    config.holdout_evidence
                ),
            )
        except retrieval_eval.EvalError as exc:
            raise ServiceError(
                f"exact retrieval holdout ledger does not match: {exc}"
            ) from exc
        if not read_client.collection_exists(config.collection):
            raise ServiceError(
                f"exact retrieval Qdrant collection is missing: {config.collection}"
            )
        if (
            str(read_client.info().version)
            != artifact_retrieval.QDRANT_SERVER_VERSION
        ):
            raise ServiceError("exact retrieval Qdrant server version does not match")
        information = read_client.get_collection(config.collection)
        collection_metadata = dict(information.config.metadata or {})
        vector_space = information.config.params.vectors
        vector_distance = getattr(vector_space, "distance", None)
        vector_distance_value = getattr(vector_distance, "value", vector_distance)
        if (
            isinstance(vector_space, dict)
            or getattr(vector_space, "size", None)
            != artifact_retrieval.EMBEDDING_DIMENSIONS
            or str(vector_distance_value).casefold() != "cosine"
        ):
            raise ServiceError(
                "exact retrieval collection vector space does not match"
            )
        expected_collection = {
            "generation": config.generation,
            "profile_digest": config.profile_digest,
            "span_manifest_digest": config.span_manifest_digest,
            "manifest_file_sha256": config.manifest_sha256,
            "embedding_model_manifest_digest": (
                config.embedding_model_manifest_digest
            ),
            "embedding_model": config.embedding_model,
            "content_payload": False,
            "current_only": True,
        }
        for key, value in expected_collection.items():
            if collection_metadata.get(key) != value:
                raise ServiceError(
                    f"exact retrieval collection metadata {key} does not match"
                )
        expected_points = lexical.metadata.get("spans")
        if not isinstance(expected_points, int) or expected_points < 0:
            raise ServiceError("exact retrieval manifest span count is invalid")
        observed_points = int(
            read_client.count(
                collection_name=config.collection,
                exact=True,
            ).count
        )
        if observed_points != expected_points:
            raise ServiceError(
                "exact retrieval collection point count does not match manifest: "
                f"{observed_points} != {expected_points}"
            )
        point_set = artifact_retrieval.verify_qdrant_point_set(
            client=read_client,
            collection=config.collection,
            manifest=config.manifest,
        )
        try:
            validated_migration_binding = (
                retrieval_eval._validate_migration_evidence(
                    migration_path,
                    manifest=config.manifest,
                    manifest_sha256=config.manifest_sha256,
                    span_manifest_digest=config.span_manifest_digest,
                    profile_digest=config.profile_digest,
                    embedding_model_manifest_digest=(
                        config.embedding_model_manifest_digest
                    ),
                    collection=config.collection,
                    generation=config.generation,
                    expected_points=expected_points,
                )
            )
        except retrieval_eval.EvalError as exc:
            raise ServiceError(
                f"exact retrieval migration evidence is invalid: {exc}"
            ) from exc
        migration_qdrant = migration.get("qdrant")
        migration_manifest = migration.get("manifest")
        migration_embedding = migration.get("embedding")
        if (
            point_set.get("vector_set_sha256")
            != migration_binding.get("vector_set_sha256")
            or validated_migration_binding != migration_binding
            or not isinstance(migration_qdrant, dict)
            or migration_qdrant.get("collection") != config.collection
            or migration_qdrant.get("generation") != config.generation
            or migration_qdrant.get("points") != expected_points
            or migration_qdrant.get("point_set") != point_set
            or not isinstance(migration_manifest, dict)
            or migration_manifest.get("file_sha256") != config.manifest_sha256
            or migration_manifest.get("span_manifest_digest")
            != config.span_manifest_digest
            or not isinstance(migration_embedding, dict)
            or migration_embedding.get("model_manifest_digest")
            != config.embedding_model_manifest_digest
        ):
            raise ServiceError(
                "exact retrieval migration/vector attestation does not match"
            )
        try:
            from fastembed import TextEmbedding
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise ServiceError("exact retrieval model dependencies are unavailable") from exc
        embedding = TextEmbedding(
            model_name=config.embedding_model,
            specific_model_path=str(config.embedding_model_snapshot),
            local_files_only=True,
            threads=artifact_retrieval.MODEL_THREADS,
        )
        reranker = TextCrossEncoder(
            config.reranker_model,
            specific_model_path=str(config.reranker_model_snapshot),
            local_files_only=True,
            threads=artifact_retrieval.MODEL_THREADS,
        )
        self.config = config
        self.lexical = lexical
        self.retriever = artifact_retrieval.HybridRetriever(
            workspace=runtime.workspace,
            catalog=runtime.catalog,
            lexical=lexical,
        )
        self.embedder = ResidentEmbedder(embedding, query_mode=True)
        self.reranker = ResidentReranker(reranker)
        self.policy = dict(policy["policy"])
        self.policy_digest = config.policy_digest
        self.system_digest = str(policy["system_digest"])
        self.point_set = point_set
        self.expected_collection = expected_collection
        self.expected_catalog = manifest_catalog
        self.catalog_path = runtime.catalog
        self.manifest_path = config.manifest
        self._freshness_lock = threading.Lock()
        self._catalog_identity = _path_identity(runtime.catalog)
        self._manifest_identity = _path_identity(config.manifest)
        self._freshness = {
            "fresh": True,
            "manifest": dict(manifest_catalog),
            "current": dict(current_catalog),
            "manifest_file_current": True,
            "error": None,
        }

    def close(self) -> None:
        self.embedder.close()
        self.lexical.close()

    def qdrant_readiness(self, read_client: Any) -> int:
        """Verify the cheap runtime invariants of the released collection."""
        if not read_client.collection_exists(self.config.collection):
            raise ServiceError("active Qdrant collection is missing")
        if (
            str(read_client.info().version)
            != artifact_retrieval.QDRANT_SERVER_VERSION
        ):
            raise ServiceError("active Qdrant server version differs from release")
        information = read_client.get_collection(self.config.collection)
        collection_metadata = dict(information.config.metadata or {})
        for key, value in self.expected_collection.items():
            if collection_metadata.get(key) != value:
                raise ServiceError(
                    f"active Qdrant collection metadata {key} differs from release"
                )
        vector_space = information.config.params.vectors
        vector_distance = getattr(vector_space, "distance", None)
        vector_distance_value = getattr(vector_distance, "value", vector_distance)
        if (
            isinstance(vector_space, dict)
            or getattr(vector_space, "size", None)
            != artifact_retrieval.EMBEDDING_DIMENSIONS
            or str(vector_distance_value).casefold() != "cosine"
        ):
            raise ServiceError(
                "active Qdrant collection vector space differs from release"
            )
        points = int(
            read_client.count(
                collection_name=self.config.collection,
                exact=True,
            ).count
        )
        if points != int(self.point_set["points"]):
            raise ServiceError(
                "active Qdrant point count differs from the released generation"
            )
        return points

    def freshness(self) -> dict[str, Any]:
        """Return the current manifest/catalog binding without hiding drift."""
        with self._freshness_lock:
            try:
                security.require_private_file(self.catalog_path)
                security.require_private_file(self.manifest_path)
                catalog_identity = _path_identity(self.catalog_path)
                manifest_identity = _path_identity(self.manifest_path)
                if (
                    catalog_identity == self._catalog_identity
                    and manifest_identity == self._manifest_identity
                ):
                    return dict(self._freshness)
                manifest_current = (
                    manifest_identity == self._manifest_identity
                    and _file_sha256(self.manifest_path)
                    == self.config.manifest_sha256
                )
                current = _catalog_binding(self.catalog_path)
                fresh = manifest_current and current == self.expected_catalog
                self._catalog_identity = catalog_identity
                self._freshness = {
                    "fresh": fresh,
                    "manifest": dict(self.expected_catalog),
                    "current": current,
                    "manifest_file_current": manifest_current,
                    "error": (
                        None
                        if fresh
                        else "catalog-or-manifest-generation-mismatch"
                    ),
                }
            except Exception as exc:
                self._freshness = {
                    "fresh": False,
                    "manifest": dict(self.expected_catalog),
                    "current": None,
                    "manifest_file_current": False,
                    "error": type(exc).__name__,
                }
            return dict(self._freshness)

    def assert_fresh(self) -> None:
        state = self.freshness()
        if not state["fresh"]:
            raise ServiceError(
                "exact retrieval catalog or manifest generation is stale"
            )


class ServiceState:
    def __init__(self, config_path: Path, health_path: Path):
        security.activate_private_umask()
        self.config_path = config_path.expanduser().absolute()
        self.health_path = health_path.expanduser().absolute()
        self.runtime = artifact_runtime.load_runtime(self.config_path)
        self.config_identity = _path_identity(self.config_path)
        self.started = time.time()
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self.metrics_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.catalog_lock = threading.Lock()
        self.health_lock = threading.Lock()
        self.last_health_write = 0.0
        self.latencies: dict[str, collections.deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=2000)
        )
        self.errors: collections.Counter[str] = collections.Counter()
        self.requests: collections.Counter[str] = collections.Counter()
        self.http_statuses: collections.Counter[str] = collections.Counter()
        self.retrieval_outcomes: collections.Counter[str] = collections.Counter()
        self.health_outcomes: collections.Counter[str] = collections.Counter()
        self.inflight_requests = 0
        self.maximum_inflight_requests = 0
        self.concurrency_rejections = 0
        self._embedded_client: Any | None = None
        self.exact: ExactRetrievalState | None = None
        self._catalog_mtime_ns = -1
        self._current_revisions: set[str] = set()

        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise ServiceError("Qdrant client dependency is unavailable") from exc
        self._qdrant_client_type = QdrantClient
        self.read_client = QdrantClient(
            url=self.runtime.qdrant_url,
            api_key=self.runtime.qdrant_read_key(),
            timeout=10,
        )
        self.embedder: ResidentEmbedder | None = None
        self.embedding_model_digest = ""
        if self.runtime.active_retrieval == "exact-hybrid-v2":
            self.exact = ExactRetrievalState(
                runtime=self.runtime,
                read_client=self.read_client,
            )
        else:
            security.require_private_file(self.runtime.build_manifest)
            build_manifest = json.loads(
                self.runtime.build_manifest.read_text(encoding="utf-8")
            )
            self.embedding_model_digest = str(
                build_manifest.get("embedding", {}).get("manifest_sha256", "")
            )
            if len(self.embedding_model_digest) != 64:
                raise ServiceError("build manifest lacks the embedding model digest")
            if self.runtime.active_backend == "server":
                self._probe_server()
            self.embedder = ResidentEmbedder(
                ingestion.create_text_embedder(ingestion.DEFAULT_EMBEDDING_MODEL)
            )
        self.write_health("starting")

    def _probe_server(self) -> None:
        if not self.read_client.collection_exists(self.runtime.qdrant_collection):
            raise ServiceError(
                f"Qdrant collection is missing: {self.runtime.qdrant_collection}"
            )

    def close(self) -> None:
        if self.exact is not None:
            self.exact.close()
        if self.embedder is not None:
            self.embedder.close()
        for client in (self._embedded_client, self.read_client):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def refresh_runtime(self) -> None:
        try:
            current_identity = _path_identity(self.config_path)
        except OSError as exc:
            raise ServiceError(f"runtime configuration is unavailable: {exc}") from exc
        if current_identity == self.config_identity:
            return
        with self.refresh_lock:
            current_identity = _path_identity(self.config_path)
            if current_identity == self.config_identity:
                return
            updated = artifact_runtime.load_runtime(self.config_path)
            immutable_fields = (
                "workspace",
                "catalog",
                "outbox_root",
                "ingestion_state",
                "consumer_state",
                "receipt_root",
                "qdrant_url",
                "qdrant_collection",
                "qdrant_generation",
                "active_retrieval",
                "retrieval",
                "qdrant_admin_key_file",
                "qdrant_read_key_file",
                "embedded_path",
                "service_socket_path",
            )
            if self.runtime.active_retrieval == "exact-hybrid-v2":
                immutable_fields += ("active_backend",)
            changed = [
                name
                for name in immutable_fields
                if getattr(updated, name) != getattr(self.runtime, name)
            ]
            if changed:
                raise ServiceError(
                    "runtime endpoint or credential paths changed; restart required: "
                    + ", ".join(changed)
                )
            if (
                self.runtime.active_backend == "embedded"
                and updated.active_backend == "server"
                and self._embedded_client is not None
            ):
                self._embedded_client.close()
                self._embedded_client = None
            self.runtime = updated
            self.config_identity = current_identity

    def active_client(self) -> tuple[Any, Path | None, str | None]:
        self.refresh_runtime()
        if self.runtime.active_backend == "server":
            return self.read_client, None, self.runtime.qdrant_url
        if self._embedded_client is None:
            self._embedded_client = self._qdrant_client_type(
                path=str(self.runtime.embedded_path)
            )
        return self._embedded_client, self.runtime.embedded_path, None

    def current_revisions(self) -> set[str]:
        mtime = self.runtime.catalog.stat().st_mtime_ns
        if mtime == self._catalog_mtime_ns:
            return self._current_revisions
        with self.catalog_lock:
            mtime = self.runtime.catalog.stat().st_mtime_ns
            if mtime != self._catalog_mtime_ns:
                _run, artifacts = ingestion.load_current_artifacts(
                    self.runtime.catalog
                )
                self._current_revisions = {
                    artifact.revision_id for artifact in artifacts
                }
                self._catalog_mtime_ns = mtime
        return self._current_revisions

    @staticmethod
    def _percentile(values: collections.deque[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
        return round(ordered[index], 3)

    def try_acquire_request_slot(self) -> bool:
        acquired = self.request_slots.acquire(blocking=False)
        with self.metrics_lock:
            if acquired:
                self.inflight_requests += 1
                self.maximum_inflight_requests = max(
                    self.maximum_inflight_requests,
                    self.inflight_requests,
                )
            else:
                self.concurrency_rejections += 1
        return acquired

    def release_request_slot(self) -> None:
        self.request_slots.release()
        with self.metrics_lock:
            self.inflight_requests -= 1

    def observe(self, route: str, duration_ms: float, status_code: int) -> None:
        with self.metrics_lock:
            self.requests[route] += 1
            self.latencies[route].append(duration_ms)
            self.http_statuses[str(status_code)] += 1
            if status_code >= 400:
                self.errors[route] += 1

    def record_retrieval_result(
        self,
        result: dict[str, Any],
        requested_limit: int,
    ) -> None:
        values = result.get("results")
        returned = len(values) if isinstance(values, list) else 0
        with self.metrics_lock:
            self.retrieval_outcomes["searches"] += 1
            if result.get("abstained") is True:
                self.retrieval_outcomes["abstentions"] += 1
            elif returned < requested_limit:
                self.retrieval_outcomes["underfilled"] += 1

    def record_integrity_conflict(self, category: str) -> None:
        with self.metrics_lock:
            self.retrieval_outcomes[category] += 1

    def record_readiness(self, available: bool) -> None:
        with self.metrics_lock:
            category = (
                "healthy_readiness_probes"
                if available
                else "degraded_readiness_probes"
            )
            self.health_outcomes[category] += 1

    def metric_summary(self) -> dict[str, Any]:
        with self.metrics_lock:
            return {
                route: {
                    "requests": int(self.requests[route]),
                    "errors": int(self.errors[route]),
                    "p50_ms": self._percentile(values, 0.50),
                    "p95_ms": self._percentile(values, 0.95),
                    "p99_ms": self._percentile(values, 0.99),
                }
                for route, values in sorted(self.latencies.items())
            }

    def operational_summary(self) -> dict[str, Any]:
        with self.metrics_lock:
            concurrency = {
                "limit": MAX_CONCURRENT_REQUESTS,
                "inflight": self.inflight_requests,
                "maximum_inflight": self.maximum_inflight_requests,
                "rejections": self.concurrency_rejections,
            }
            statuses = dict(sorted(self.http_statuses.items()))
            retrieval = {
                category: int(self.retrieval_outcomes[category])
                for category in (
                    "searches",
                    "abstentions",
                    "underfilled",
                    "catalog_or_manifest_conflicts",
                    "hash_or_source_conflicts",
                )
            }
            health = {
                category: int(self.health_outcomes[category])
                for category in (
                    "healthy_readiness_probes",
                    "degraded_readiness_probes",
                )
            }
        embedder = self.exact.embedder if self.exact is not None else self.embedder
        queues: dict[str, Any] = {
            "embedding": (
                embedder.metrics()
                if embedder is not None and hasattr(embedder, "metrics")
                else None
            ),
            "reranker": (
                self.exact.reranker.metrics()
                if self.exact is not None
                and hasattr(self.exact.reranker, "metrics")
                else None
            ),
        }
        return {
            "http_statuses": statuses,
            "retrieval": retrieval,
            "health": health,
            "concurrency": concurrency,
            "queues": queues,
        }

    def active_identity(self) -> tuple[str, str]:
        if self.exact is not None:
            return self.exact.config.collection, self.exact.config.generation
        collection = (
            self.runtime.qdrant_collection
            if self.runtime.active_backend == "server"
            else ingestion.DEFAULT_COLLECTION
        )
        return collection, self.runtime.qdrant_generation

    def readiness(self) -> dict[str, Any]:
        """Bounded readiness probe for health files and the loopback endpoint."""
        collection = generation = "unknown"
        freshness: dict[str, Any] | None = None
        try:
            self.refresh_runtime()
            collection, generation = self.active_identity()
            if self.exact is not None:
                freshness = self.exact.freshness()
                if not freshness["fresh"]:
                    raise ServiceError(
                        "exact retrieval catalog or manifest generation is stale"
                    )
                client = self.read_client
                points = self.exact.qdrant_readiness(client)
            else:
                client, _local_path, _url = self.active_client()
                if not client.collection_exists(collection):
                    raise ServiceError("active Qdrant collection is missing")
                points = int(
                    client.count(
                        collection_name=collection,
                        exact=True,
                    ).count
                )
            result = {
                "available": True,
                "status": "healthy",
                "active_backend": self.runtime.active_backend,
                "active_retrieval": self.runtime.active_retrieval,
                "collection": collection,
                "generation": generation,
                "points": points,
                "catalog_freshness": freshness,
                "error": None,
            }
            self.record_readiness(True)
            return result
        except Exception as exc:
            result = {
                "available": False,
                "status": "degraded",
                "active_backend": self.runtime.active_backend,
                "active_retrieval": self.runtime.active_retrieval,
                "collection": collection,
                "generation": generation,
                "points": None,
                "catalog_freshness": freshness,
                "error": type(exc).__name__,
            }
            self.record_readiness(False)
            return result

    def write_health(
        self,
        status: str,
        error: str | None = None,
        *,
        force: bool = False,
    ) -> None:
        now = time.time()
        with self.health_lock:
            if not force and now - self.last_health_write < 5.0:
                return
            self.last_health_write = now
        readiness: dict[str, Any] | None = None
        if status == "healthy":
            readiness = self.readiness()
            if not readiness["available"]:
                status = "degraded"
                error = str(readiness["error"])
        collection, generation = self.active_identity()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "pid": os.getpid(),
            "started_unix": self.started,
            "updated_unix": now,
            "active_backend": self.runtime.active_backend,
            "active_retrieval": self.runtime.active_retrieval,
            "generation": generation,
            "collection": collection,
            "error": error,
            "readiness": readiness,
            "operational_metrics": self.operational_summary(),
        }
        security.atomic_write_json(self.health_path, payload, replace=True)

    def status(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del body
        self.refresh_runtime()
        from qdrant_client import models

        available = True
        qdrant_error: str | None = None
        total = current = historical = 0
        active_collection, active_generation = self.active_identity()
        freshness = self.exact.freshness() if self.exact is not None else None
        try:
            client = (
                self.read_client
                if self.exact is not None
                else self.active_client()[0]
            )
            total = int(
                client.count(
                    collection_name=active_collection,
                    exact=True,
                ).count
            )
            current = int(
                client.count(
                    collection_name=active_collection,
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="catalog_current",
                                match=models.MatchValue(value=True),
                            )
                        ]
                    ),
                    exact=True,
                ).count
            )
            historical = total - current
            if self.exact is not None:
                self.exact.qdrant_readiness(self.read_client)
        except Exception as exc:
            available = False
            qdrant_error = f"{type(exc).__name__}: {exc}"[:500]
        catalog = artifact_memory._catalog_status(self.runtime.catalog)
        graphiti = artifact_memory._graphiti_status(
            "127.0.0.1",
            6379,
            "FALKORDB_PASSWORD",
            artifact_memory.DEFAULT_APPROVALS,
        )
        snapshot_age_seconds: float | None = None
        if self.runtime.build_manifest.exists():
            snapshot_age_seconds = max(
                0.0, time.time() - self.runtime.build_manifest.stat().st_mtime
            )
        catalog_available = catalog.get("available") is True
        catalog_fresh = freshness is None or freshness["fresh"] is True
        service_available = available and catalog_available and catalog_fresh
        degraded_reasons: list[str] = []
        if not available:
            degraded_reasons.append("qdrant")
        if not catalog_available:
            degraded_reasons.append("catalog")
        if not catalog_fresh:
            degraded_reasons.append("catalog-generation")
        return {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "service_build": SERVICE_BUILD,
            "mode": "resident-service-status",
            "service": {
                "available": service_available,
                "status": "healthy" if service_available else "degraded",
                "degraded_reasons": degraded_reasons,
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - self.started, 1),
                "active_backend": self.runtime.active_backend,
                "active_retrieval": self.runtime.active_retrieval,
                "max_concurrent_requests": MAX_CONCURRENT_REQUESTS,
                # None where the platform cannot report it. Previously this was
                # `int(ru_maxrss)`, which is bytes on macOS but KILOBYTES on
                # Linux — the field was 1024x low on every Linux host.
                "rss_peak_bytes": platform_compat.peak_rss_bytes(),
                "metrics": self.metric_summary(),
                "operational_metrics": self.operational_summary(),
            },
            "catalog": catalog,
            "qdrant": {
                "available": available,
                "error": qdrant_error,
                "url": "loopback",
                "collection": active_collection,
                "generation": active_generation,
                "points": total,
                "current_points": current,
                "historical_points": historical,
                "snapshot_age_seconds": snapshot_age_seconds,
            },
            "graphiti": graphiti,
            "skill_receipts": {
                "count": artifact_memory._receipt_count(self.runtime.receipt_root),
                "consumer_status": artifact_memory._consumer_status(
                    self.runtime.consumer_state
                ),
            },
            "read_controls": {
                "historical_snippets": "disabled",
                "unapproved_graphiti_pilots": "disabled",
                "shared_profile_exposure": "disabled-at-adapter",
            },
            "source_mutation": "disabled",
            "direct_qdrant_credentials": "service-only",
            "retrieval_release": (
                {
                    "collection": self.exact.config.collection,
                    "generation": self.exact.config.generation,
                    "ranking_version": self.exact.config.ranking_version,
                    "manifest_sha256": self.exact.config.manifest_sha256,
                    "profile_digest": self.exact.config.profile_digest,
                    "span_manifest_digest": (
                        self.exact.config.span_manifest_digest
                    ),
                    "embedding_model_manifest_digest": (
                        self.exact.config.embedding_model_manifest_digest
                    ),
                    "reranker_model_manifest_digest": (
                        self.exact.config.reranker_model_manifest_digest
                    ),
                    "policy_digest": self.exact.policy_digest,
                    "system_digest": self.exact.system_digest,
                    "point_set": self.exact.point_set,
                    "catalog_freshness": freshness,
                    "development_evidence_digest": (
                        self.exact.config.development_evidence_digest
                    ),
                    "holdout_evidence_digest": (
                        self.exact.config.holdout_evidence_digest
                    ),
                    "incremental_publication": "disabled-fail-closed",
                }
                if self.exact is not None
                else {
                    "ranking_version": "legacy-vector-v1",
                    "incremental_publication": "legacy-only",
                }
            ),
        }

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        self.refresh_runtime()
        query = _required_string(body, "query", 1000)
        limit = _bounded_int(body, "limit", 8, 1, 25)
        if body.get("include_history") not in (None, False):
            raise artifact_memory.MemoryReadError(
                artifact_memory.HISTORICAL_SNIPPETS_DISABLED
            )
        filters = {
            name: _optional_string(body, name, 500)
            for name in (
                "project",
                "artifact_type",
                "authority_class",
                "repository",
                "lifecycle_hint",
            )
        }
        if self.exact is not None:
            try:
                self.exact.assert_fresh()
            except ServiceError:
                self.record_integrity_conflict("catalog_or_manifest_conflicts")
                raise
            vector = self.exact.embedder.embed_query(query)
            try:
                result = self.exact.retriever.search(
                    client=self.read_client,
                    collection=self.exact.config.collection,
                    query=query,
                    query_vector=vector.tolist(),
                    limit=limit,
                    mode="hybrid-rerank",
                    reranker=self.exact.reranker,
                    abstention_policy=self.exact.policy,
                    **filters,
                )
            except artifact_retrieval.RetrievalError as exc:
                text = str(exc).casefold()
                if any(
                    marker in text
                    for marker in (
                        "hash",
                        "canonical",
                        "coordinate",
                        "revision",
                        "changed",
                    )
                ):
                    self.record_integrity_conflict("hash_or_source_conflicts")
                raise
            result.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "retrieval_path": "resident-exact-hybrid-v2",
                    "generation": self.exact.config.generation,
                    "active_backend": self.runtime.active_backend,
                    "active_retrieval": self.runtime.active_retrieval,
                    "authority": "current-canonical-span",
                    "verification": "source-and-span-hash-verified",
                    "source_mutation": "disabled",
                }
            )
            self.record_retrieval_result(result, limit)
            return result
        client, local_path, url = self.active_client()
        if self.embedder is None:
            raise ServiceError("legacy retrieval embedder is unavailable")
        vector = self.embedder.embed_query(query)
        active_collection = (
            self.runtime.qdrant_collection
            if self.runtime.active_backend == "server"
            else ingestion.DEFAULT_COLLECTION
        )
        result = ingestion.qdrant_search(
            workspace=self.runtime.workspace,
            catalog=self.runtime.catalog,
            local_path=local_path,
            url=url,
            api_key_env="QDRANT_API_KEY",
            api_key=(
                self.runtime.qdrant_read_key()
                if self.runtime.active_backend == "server"
                else None
            ),
            client=client,
            query_vector=vector.tolist(),
            current_revisions_override=self.current_revisions(),
            verify_lifecycle_payload=False,
            collection=active_collection,
            embedding_model=ingestion.DEFAULT_EMBEDDING_MODEL,
            query=query,
            limit=limit,
            current_only=True,
            **filters,
        )
        result.update(
            {
                "schema_version": SCHEMA_VERSION,
                "retrieval_path": "resident-vector-v1",
                "generation": self.runtime.qdrant_generation,
                "active_backend": self.runtime.active_backend,
                "active_retrieval": self.runtime.active_retrieval,
                "authority": "discovery-only",
                "verification": "read returned relative_path with get_artifact",
                "source_mutation": "disabled",
            }
        )
        self.record_retrieval_result(result, limit)
        return result

    def get(self, body: dict[str, Any]) -> dict[str, Any]:
        return artifact_memory.get_artifact(
            workspace=self.runtime.workspace,
            catalog=self.runtime.catalog,
            relative_path=_required_string(body, "relative_path", 2000),
            max_chars=_bounded_int(body, "max_chars", 12_000, 256, 50_000),
        )

    def facts(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("include_pilot") not in (None, False):
            raise artifact_memory.MemoryReadError(
                artifact_memory.PILOT_ACCESS_DISABLED
            )
        return artifact_memory.temporal_facts(
            query=_required_string(body, "query", 1000),
            limit=_bounded_int(body, "limit", 8, 1, 25),
            group_id=_optional_string(body, "group_id", 500),
            include_pilot=False,
            approvals=artifact_memory.DEFAULT_APPROVALS,
            host="127.0.0.1",
            port=6379,
            password_env="FALKORDB_PASSWORD",
        )

    def ingest(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        raise ServiceError(
            "the Artifact Memory Service listener is read-only; "
            "derived ingestion requires the separate operator path"
        )

    def reconcile(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        raise ServiceError(
            "the Artifact Memory Service listener is read-only; "
            "derived reconciliation requires the separate operator path"
        )


def _required_string(body: dict[str, Any], name: str, maximum: int) -> str:
    value = body.get(name)
    if not isinstance(value, str):
        raise ServiceError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum or "\0" in value:
        raise ServiceError(f"{name} must contain 1-{maximum} safe characters")
    return value


def _optional_string(
    body: dict[str, Any], name: str, maximum: int
) -> str | None:
    if name not in body or body[name] is None:
        return None
    return _required_string(body, name, maximum)


def _bounded_int(
    body: dict[str, Any],
    name: str,
    fallback: int,
    minimum: int,
    maximum: int,
) -> int:
    value = body.get(name, fallback)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ServiceError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _socket_identity(information: os.stat_result) -> tuple[int, int]:
    return information.st_dev, information.st_ino


def _socket_lifecycle_lock_path(socket_path: Path) -> Path:
    """Return the permanent flock backing file for one socket pathname.

    The file deliberately outlives a service process.  Unlinking a lock file
    during shutdown can split a live advisory lock across two inodes when a
    concurrent launcher opens a replacement pathname.
    """
    return socket_path.with_name(f".{socket_path.name}.lock")


if hasattr(socketserver, "UnixStreamServer"):
    _UnixStreamServerBase = socketserver.UnixStreamServer
else:
    # Windows has no AF_UNIX, so `socketserver` does not define
    # UnixStreamServer and the class statement below was an AttributeError at
    # IMPORT time — which is why this module, and the ten test modules that
    # reach it, never collected off POSIX (M2, gates-green-t-fcntl-substrate).
    #
    # The stub makes the module importable and nothing more: constructing the
    # server still fails, with a named error that says why and what to do,
    # rather than a NameError or a server that pretends to listen. Porting the
    # transport is M5 (native-everywhere); WSL2 is the supported Windows path
    # until then.
    class _UnixStreamServerBase(socketserver.BaseServer):  # type: ignore[misc]
        """Import-time placeholder for socketserver.UnixStreamServer."""

        def __init__(self, *args, **kwargs):
            raise ServiceError(
                "the resident Artifact Memory Service needs a Unix-domain socket, "
                f"and {sys.platform} has no AF_UNIX support. Run the service inside "
                "WSL2 (see docs/platforms/windows-wsl.md); a native Windows "
                "transport is tracked as M5."
            )


class ArtifactUnixHTTPServer(socketserver.ThreadingMixIn, _UnixStreamServerBase):
    """HTTP/1.1 over an owner-private pathname Unix-domain socket."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, socket_path: Path, state: ServiceState):
        try:
            self.socket_path = artifact_runtime._future_private_socket(
                str(socket_path),
                "service socket",
            )
        except artifact_runtime.RuntimeConfigError as exc:
            raise ServiceError(
                f"refusing unsafe existing service socket: {exc}"
            ) from exc
        self.state = state
        self._bound_socket_identity: tuple[int, int] | None = None
        self._socket_lifecycle_lock_file = _socket_lifecycle_lock_path(
            self.socket_path
        )
        self._socket_lifecycle_lock_descriptor: int | None = None
        security.activate_private_umask()
        self._acquire_socket_lifecycle_lock()
        try:
            self._prepare_socket_path()
            super().__init__(str(self.socket_path), ArtifactRequestHandler)
            os.chmod(self.socket_path, artifact_runtime.PRIVATE_SOCKET_MODE)
            information = artifact_runtime.require_private_socket(
                self.socket_path,
                "service socket",
            ).lstat()
            self._bound_socket_identity = _socket_identity(information)
        except BaseException:
            if hasattr(self, "socket"):
                socketserver.UnixStreamServer.server_close(self)
            self._unlink_bound_socket()
            self._release_socket_lifecycle_lock()
            raise

    def _acquire_socket_lifecycle_lock(self) -> None:
        """Hold an owner-private flock through prepare, bind, and cleanup."""
        flags = (
            os.O_RDWR | getattr(os, "O_BINARY", 0)
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(
                self._socket_lifecycle_lock_file,
                flags,
                security.PRIVATE_FILE_MODE,
            )
        except OSError as exc:
            raise ServiceError(
                f"could not open service socket lifecycle lock: {exc}"
            ) from exc
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode) or not security._owner_and_mode_ok(
                information, self._socket_lifecycle_lock_file
            ):
                raise ServiceError("refusing unsafe service socket lifecycle lock")
            try:
                security.require_private_file(self._socket_lifecycle_lock_file)
                named = self._socket_lifecycle_lock_file.lstat()
            except (OSError, security.PrivateStateError) as exc:
                raise ServiceError(
                    f"refusing unsafe service socket lifecycle lock: {exc}"
                ) from exc
            if _socket_identity(named) != _socket_identity(information):
                raise ServiceError("service socket lifecycle lock changed while opening")
            try:
                if not platform_compat.try_lock_exclusive(descriptor):
                    raise ServiceError(
                        "Artifact Memory Service socket startup is already in progress"
                    )
            except OSError as exc:
                raise ServiceError(
                    f"could not acquire service socket lifecycle lock: {exc}"
                ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._socket_lifecycle_lock_descriptor = descriptor

    def _release_socket_lifecycle_lock(self) -> None:
        """Release the advisory lock without unlinking its backing inode."""
        descriptor = self._socket_lifecycle_lock_descriptor
        self._socket_lifecycle_lock_descriptor = None
        if descriptor is None:
            return
        try:
            platform_compat.unlock_file(descriptor)
        finally:
            os.close(descriptor)

    def _prepare_socket_path(self) -> None:
        """Refuse active endpoints and unlink only a verified stale socket."""
        try:
            before = self.socket_path.lstat()
        except FileNotFoundError:
            return
        try:
            artifact_runtime.require_private_socket(self.socket_path, "service socket")
        except artifact_runtime.RuntimeConfigError as exc:
            raise ServiceError(f"refusing unsafe existing service socket: {exc}") from exc

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                raise ServiceError(
                    f"could not verify existing service socket: {exc}"
                ) from exc
            if exc.errno == errno.ENOENT:
                return
        else:
            raise ServiceError("Artifact Memory Service socket is already active")
        finally:
            probe.close()

        try:
            current = artifact_runtime.require_private_socket(
                self.socket_path,
                "service socket",
            ).lstat()
        except artifact_runtime.RuntimeConfigError as exc:
            raise ServiceError(f"service socket changed before cleanup: {exc}") from exc
        if _socket_identity(current) != _socket_identity(before):
            raise ServiceError("service socket changed before stale cleanup")
        self.socket_path.unlink()
        security.fsync_directory(self.socket_path.parent)

    def _unlink_bound_socket(self) -> None:
        expected = self._bound_socket_identity
        if expected is None:
            return
        try:
            current = artifact_runtime.require_private_socket(
                self.socket_path,
                "service socket",
            ).lstat()
        except artifact_runtime.RuntimeConfigError:
            return
        # Unlinking someone else's socket is the risk this guards; where
        # st_uid cannot answer that, the socket identity match is what remains.
        if not platform_compat.supports_posix_privacy():
            platform_compat.owner_check_degraded(
                "artifact_memory_service._unlink_stale_socket", str(self.socket_path)
            )
            owner_ok = True
        else:
            owner_ok = current.st_uid == platform_compat.current_uid()
        if (
            stat.S_ISSOCK(current.st_mode)
            and _socket_identity(current) == expected
            and owner_ok
        ):
            self.socket_path.unlink()
            try:
                security.fsync_directory(self.socket_path.parent)
            except OSError:
                pass

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            try:
                self._unlink_bound_socket()
            finally:
                self._release_socket_lifecycle_lock()


class ArtifactRequestHandler(BaseHTTPRequestHandler):
    server: ArtifactUnixHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Return and account for protocol-level errors as bounded JSON."""
        del message, explain
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._response_status = int(status)
        self._send(status, {"error": status.phrase.casefold()})
        path = getattr(self, "path", "")
        route = path if path in PUBLIC_ROUTES else "__unknown__"
        self.server.state.observe(route, 0.0, self._response_status)
        if self._response_status >= 500:
            try:
                self.server.state.write_health("degraded", status.phrase)
            except Exception:
                pass

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = (
            json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            raw = b'{"error":"response exceeds service limit"}\n'
        self._response_status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Every response -- including protocol-level errors -- carries the
        # running contract identity so a client that loaded an older adapter
        # can detect the upgrade instead of misparsing a newer shape.
        self.send_header(PROTOCOL_HEADER, str(PROTOCOL_VERSION))
        self.send_header(SCHEMA_HEADER, str(SCHEMA_VERSION))
        self.send_header(BUILD_HEADER, SERVICE_BUILD)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _drain_request_body(self) -> None:
        """Consume a pending request body before an early rejection.

        Replying and closing while the peer is still writing turns a precise
        426 into a broken pipe at the client -- the opposite of the actionable
        error this fence exists to deliver. The drain is bounded by the same
        limit the served path allows, so a fenced client costs no more than an
        accepted one.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(max(length, 0), MAX_REQUEST_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_GET(self) -> None:
        started = time.perf_counter()
        route = "/healthz" if self.path == "/healthz" else "__unknown__"
        acquired = False
        self._response_status = int(HTTPStatus.INTERNAL_SERVER_ERROR)
        readiness: dict[str, Any] | None = None
        try:
            if self.path != "/healthz":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self.server.state.try_acquire_request_slot():
                self._send(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "service concurrency limit reached"},
                )
                return
            acquired = True
            readiness = self.server.state.readiness()
            self._send(
                (
                    HTTPStatus.OK
                    if readiness["available"]
                    else HTTPStatus.SERVICE_UNAVAILABLE
                ),
                {
                    "status": readiness["status"],
                    "schema_version": SCHEMA_VERSION,
                    "protocol_version": PROTOCOL_VERSION,
                    "service_build": SERVICE_BUILD,
                    "service": "artifact-memory",
                    "active_backend": readiness["active_backend"],
                    "active_retrieval": readiness["active_retrieval"],
                    "collection": readiness["collection"],
                    "generation": readiness["generation"],
                    "catalog_fresh": (
                        readiness["catalog_freshness"] is None
                        or readiness["catalog_freshness"]["fresh"] is True
                    ),
                },
            )
        finally:
            if acquired:
                self.server.state.release_request_slot()
            duration = (time.perf_counter() - started) * 1000
            self.server.state.observe(route, duration, self._response_status)
            try:
                self.server.state.write_health(
                    (
                        "degraded"
                        if self._response_status >= 500
                        or self._response_status == HTTPStatus.TOO_MANY_REQUESTS
                        else "healthy"
                    ),
                    None,
                )
            except Exception:
                pass

    def do_POST(self) -> None:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        route = self.path
        metric_route = route if route in PUBLIC_ROUTES else "__unknown__"
        acquired = False
        response_payload: dict[str, Any] | None = None
        self._response_status = int(HTTPStatus.INTERNAL_SERVER_ERROR)
        try:
            if self.headers.get("Origin") is not None:
                self._send(
                    HTTPStatus.FORBIDDEN,
                    {"error": "browser origins are rejected"},
                )
                return
            # Fence the wire protocol before any slot, body read, or dispatch:
            # a client this build cannot serve must get an explicit, actionable
            # 426 rather than a handler running against a foreign request shape.
            try:
                require_supported_protocol(self.headers.get(PROTOCOL_HEADER))
            except ProtocolVersionError as exc:
                self._drain_request_body()
                self._send(
                    HTTPStatus.UPGRADE_REQUIRED,
                    {
                        "error": str(exc)[:1000],
                        "schema_version": SCHEMA_VERSION,
                        "service_protocol_version": PROTOCOL_VERSION,
                        "service_build": SERVICE_BUILD,
                        "action": "restart-session",
                    },
                )
                return
            if not self.server.state.try_acquire_request_slot():
                self._send(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "service concurrency limit reached"},
                )
                return
            acquired = True
            length_text = self.headers.get("Content-Length")
            if length_text is None:
                raise ServiceError("Content-Length is required")
            length = int(length_text)
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise ServiceError("request body exceeds service limit")
            if self.headers.get_content_type() != "application/json":
                raise ServiceError("Content-Type must be application/json")
            raw = self.rfile.read(length)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ServiceError("request body must be a JSON object")
            handlers = {
                "/v1/status": self.server.state.status,
                "/v1/search": self.server.state.search,
                "/v1/get": self.server.state.get,
                "/v1/facts": self.server.state.facts,
            }
            handler = handlers.get(route)
            if handler is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            response_payload = handler(body)
            self._send(HTTPStatus.OK, response_payload)
        except (
            ServiceError,
            artifact_memory.MemoryReadError,
            artifact_retrieval.RetrievalError,
            ingestion.IngestionError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": str(exc)[:1000], "request_id": request_id},
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:1000]
            print(
                json.dumps(
                    {"level": "error", "request_id": request_id, "error": detail}
                ),
                file=sys.stderr,
                flush=True,
            )
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "service operation failed", "request_id": request_id},
            )
        finally:
            if acquired:
                self.server.state.release_request_slot()
            duration = (time.perf_counter() - started) * 1000
            self.server.state.observe(
                metric_route,
                duration,
                self._response_status,
            )
            try:
                status_unavailable = (
                    route == "/v1/status"
                    and isinstance(response_payload, dict)
                    and isinstance(response_payload.get("service"), dict)
                    and response_payload["service"].get("available") is False
                )
                self.server.state.write_health(
                    (
                        "degraded"
                        if self._response_status >= 500
                        or self._response_status == HTTPStatus.TOO_MANY_REQUESTS
                        or status_unavailable
                        else "healthy"
                    ),
                    None,
                )
            except Exception:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=artifact_runtime.DEFAULT_CONFIG,
    )
    parser.add_argument("--health-file", type=Path, default=DEFAULT_HEALTH)
    parser.add_argument("--check", action="store_true")
    return parser


#: Platforms on which the resident service can actually listen. The transport is
#: an AF_UNIX socket; a native Windows port (named pipe or loopback TCP) is M5.
SERVICE_SUPPORTED_PLATFORMS = ("linux", "darwin")


def _refuse_unsupported_platform() -> str | None:
    """The message to print, or None if this platform can run the service.

    Fails FAST and by NAME. Before this, an unsupported host got whatever error
    happened to surface first from deep in socket setup — a message about socket
    paths that told the operator nothing about the real problem, which is that
    the platform has no AF_UNIX at all (M2, gates-green-t-supported-platforms).
    """
    if sys.platform in SERVICE_SUPPORTED_PLATFORMS:
        return None
    return (
        f"artifact-memory-service: unsupported platform: {sys.platform}.\n"
        f"  The resident service listens on a Unix-domain socket, and this "
        f"platform has no AF_UNIX support.\n"
        f"  Supported natively: {', '.join(SERVICE_SUPPORTED_PLATFORMS)}.\n"
        # ASCII only: this message is read on a Windows console, whose default
        # code page renders an em dash as a replacement character.
        f"  On Windows, run it inside WSL2 - see docs/platforms/windows-wsl.md.\n"
        f"  A native Windows transport is milestone M5 (native-everywhere).\n"
        f"  Everything else in this repository (the MCP server, the generators "
        f"and the substrate test suite) does run here."
    )


def main(argv: Sequence[str] | None = None) -> int:
    refusal = _refuse_unsupported_platform()
    if refusal is not None:
        sys.stderr.write(refusal + "\n")
        return 2

    args = _parser().parse_args(argv)
    state: ServiceState | None = None
    server: ArtifactUnixHTTPServer | None = None
    try:
        state = ServiceState(args.config, args.health_file)
        if args.check:
            print(json.dumps(state.status(), indent=2, sort_keys=True))
            return 0
        server = ArtifactUnixHTTPServer(state.runtime.service_socket_path, state)

        def stop(_signum: int, _frame: Any) -> None:
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        state.write_health("healthy", force=True)
        server.serve_forever(poll_interval=0.5)
        state.write_health("stopped", force=True)
        return 0
    except Exception as exc:
        if state is not None:
            try:
                state.write_health(
                    "failed",
                    f"{type(exc).__name__}: {exc}"[:1000],
                    force=True,
                )
            except Exception:
                pass
        print(f"artifact-memory-service: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
