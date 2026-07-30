#!/usr/bin/env python3
"""Evaluate one frozen exact-span retrieval generation without network access."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sqlite3
import statistics
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import artifact_evidence as evidence_schema
import artifact_ingestion as ingestion
import artifact_retrieval as retrieval
import artifact_retrieval_migrate as migration
import artifact_runtime
import artifact_security as security


SCHEMA_VERSION = 1
GOLD_VERSION = "retrieval-gold-v1"
MODES = ("vector", "lexical", "hybrid", "hybrid-rerank")
TOP_K = 10
EXPECTED_RECORDS_PER_SPLIT = 30
EXPECTED_ANSWERABLE_PER_SPLIT = 20
EXPECTED_NEGATIVE_PER_SPLIT = 10
EMBEDDING_MODEL = retrieval.EMBEDDING_MODEL
RERANKER_MODEL = retrieval.RERANKER_MODEL
DEFAULT_MANIFEST = (
    artifact_runtime.DEFAULT_DERIVED_ROOT
    / "artifact-spans-p20260721v2.sqlite3"
)
DEFAULT_COLLECTION = "personal_artifact_spans_p20260721v2"
DEFAULT_POLICY = (
    artifact_runtime.DEFAULT_DERIVED_ROOT
    / "artifact-retrieval-policy-p20260721v2.json"
)
MATERIAL_SOURCE_FILES = (
    "artifact_ingestion.py",
    "artifact_memory.py",
    "artifact_memory_service.py",
    "artifact_retrieval.py",
    "artifact_retrieval_eval.py",
    "artifact_retrieval_migrate.py",
    "artifact_runtime.py",
    "artifact_security.py",
    "artifact_span_generation.py",
)
MATERIAL_ADAPTER_FILES = (
    ("claude-mcp-server/package.json", "package.json"),
    ("claude-mcp-server/package-lock.json", "package-lock.json"),
    ("claude-mcp-server/src/config.ts", "src/config.ts"),
    ("claude-mcp-server/src/index.ts", "src/index.ts"),
    (
        "claude-mcp-server/src/tools/artifact-memory.ts",
        "src/tools/artifact-memory.ts",
    ),
    ("claude-mcp-server/dist/config.js", "dist/config.js"),
    ("claude-mcp-server/dist/index.js", "dist/index.js"),
    (
        "claude-mcp-server/dist/tools/artifact-memory.js",
        "dist/tools/artifact-memory.js",
    ),
)
RELEASE_POLICY_KEYS = {
    "schema_version",
    "ranking_version",
    "policy",
    "policy_digest",
    "system",
    "system_digest",
    "development_gold_digest",
    "development_selection_feasible",
    "development_evidence",
}
DEVELOPMENT_EVIDENCE_BINDING_KEYS = {
    "path",
    "file_sha256",
    "evidence_digest",
}
CURRENT_ONLY_SCOPE = "local_owner/current_only"
CURRENT_ONLY_FILTERS = {
    "project": None,
    "artifact_type": None,
    "authority_class": None,
    "repository": None,
    "lifecycle_hint": None,
}

TOP_KEYS = {
    "schema_version",
    "gold_version",
    "generation",
    "span_manifest_digest",
    "split_methodology",
    "adjudication",
    "split",
    "records",
}
GOLD_MANIFEST_KEYS = {
    "schema_version",
    "gold_version",
    "generation",
    "span_manifest_digest",
    "split_methodology",
    "adjudication",
    "splits",
}
GOLD_MANIFEST_SPLIT_KEYS = {"file", "sha256", "record_count"}
RECORD_KEYS = {
    "query_id",
    "split",
    "query",
    "categories",
    "answerable",
    "expected_relative_path",
    "expected_revision_id",
    "old_chunk_sha256",
    "expected_heading",
    "hard_negative_paths",
    "allowed_alternate_paths",
    "scope",
    "adjudication",
    "judgments",
}
JUDGMENT_KEYS = {
    "relative_path",
    "revision_id",
    "span_id",
    "point_id",
    "byte_start",
    "byte_end",
    "span_sha256",
    "grade",
}


class EvalError(RuntimeError):
    """An evaluation input or invariant is invalid."""


@dataclass(frozen=True)
class GoldSuite:
    records: tuple[dict[str, Any], ...]
    split: str
    gold_digest: str
    generation: str
    span_manifest_digest: str

    @property
    def dev(self) -> tuple[dict[str, Any], ...]:
        return self.records if self.split == "dev" else ()

    @property
    def holdout(self) -> tuple[dict[str, Any], ...]:
        return self.records if self.split == "holdout" else ()

    @property
    def dev_digest(self) -> str | None:
        return self.gold_digest if self.split == "dev" else None

    @property
    def holdout_digest(self) -> str | None:
        return self.gold_digest if self.split == "holdout" else None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        digest, entries = security.private_tree_manifest(path)
    except security.PrivateStateError as exc:
        raise EvalError(f"model snapshot is unsafe: {exc}") from exc
    return entries, digest


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


ADAPTER_ROOT_ENV = "PERSONAL_ADAPTER_ROOT"
ADAPTER_PACKAGE_NAME = "@chris-dare-dev/agent-kit"


def _is_adapter_root(candidate: Path) -> bool:
    """True if `candidate` is the claude-mcp-server checkout.

    Identified by its package.json `name`, not by directory name — a path
    component is not evidence, and binding the wrong tree's files into a
    release manifest would be silent, not loud.
    """
    manifest = candidate / "package.json"
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError):
        return False
    return declared == ADAPTER_PACKAGE_NAME


def _resolve_adapter_root(root: Path) -> Path:
    """Locate the adapter checkout without assuming where the substrate lives.

    The manifest binds this substrate's modules AND the claude-mcp-server
    adapter files, so it needs both trees. Until 2026-07-18 the adapter was
    found by a hardcoded offset from `<workspace>/scripts/`, which made the
    substrate unmovable: relocating it doubled the path
    (`…/claude-mcp-server/GitLab/…/claude-mcp-server/package.json`) and failed
    23 of 433 tests. Resolution order — explicit, legacy, discovered:

      1. $PERSONAL_ADAPTER_ROOT — explicit override for packaging or air-gapped
         layouts where neither heuristic applies.
      2. The legacy offset, so an in-place substrate keeps its exact behavior.
      3. Walk up from the substrate looking for the adapter package. This is
         what makes the substrate relocatable, including INTO the adapter repo
         (F-10), where the adapter root is simply an ancestor.

    Note `root` is already `.resolve()`d by the caller, so a symlinked
    substrate directory resolves to its real location before the walk — the
    walk cannot be redirected by symlinking the substrate.
    """
    override = os.environ.get(ADAPTER_ROOT_ENV)
    if override:
        candidate = Path(override).expanduser()
        if not _is_adapter_root(candidate):
            raise EvalError(
                f"{ADAPTER_ROOT_ENV}={override!r} is not a {ADAPTER_PACKAGE_NAME} "
                "checkout (no package.json with that name)"
            )
        return candidate

    for ancestor in (root, *root.parents):
        if _is_adapter_root(ancestor):
            return ancestor

    raise EvalError(
        f"cannot locate the {ADAPTER_PACKAGE_NAME} checkout from {root}: "
        f"tried {ADAPTER_ROOT_ENV} and every ancestor. "
        f"Set {ADAPTER_ROOT_ENV} to the checkout path."
    )


def _material_source_manifest() -> dict[str, Any]:
    """Bind every local source file that can change release behavior."""
    root = Path(__file__).resolve().parent
    adapter_root = _resolve_adapter_root(root)
    candidates = [
        (name, root / name) for name in MATERIAL_SOURCE_FILES
    ] + [
        (label, adapter_root / relative)
        for label, relative in MATERIAL_ADAPTER_FILES
    ]
    entries: list[dict[str, Any]] = []
    for name, candidate in candidates:
        if candidate.is_symlink():
            raise EvalError(f"material release source is a symlink: {candidate}")
        try:
            before = candidate.stat()
        except OSError as exc:
            raise EvalError(f"material release source is unavailable: {candidate}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise EvalError(f"material release source is not regular: {candidate}")
        file_digest = _file_sha256(candidate)
        after = candidate.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EvalError(f"material release source changed during read: {candidate}")
        entries.append(
            {
                "path": name,
                "bytes": int(after.st_size),
                "sha256": file_digest,
            }
        )
    entries.sort(key=lambda value: str(value["path"]))
    return {
        "files": entries,
        "digest": _sha256_bytes(_canonical_json(entries)),
    }


def _evaluation_runtime_contract() -> dict[str, Any]:
    dependencies: dict[str, str] = {}
    for distribution in (
        "fastembed",
        "numpy",
        "onnxruntime",
        "qdrant-client",
        "tokenizers",
    ):
        try:
            dependencies[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise EvalError(
                f"evaluation dependency is unavailable: {distribution}"
            ) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "gold_version": GOLD_VERSION,
        "modes": list(MODES),
        "top_k": TOP_K,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "sqlite_version": sqlite3.sqlite_version,
        "dependencies": dependencies,
    }


def _catalog_release_binding(
    catalog: Path,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "catalog_run_id": metadata.get("catalog_run_id"),
        "catalog_artifacts": metadata.get("catalog_artifacts"),
        "catalog_revision_set_sha256": metadata.get(
            "catalog_revision_set_sha256"
        ),
    }
    if (
        metadata.get("catalog_status") != "complete"
        or isinstance(expected["catalog_run_id"], bool)
        or not isinstance(expected["catalog_run_id"], int)
        or isinstance(expected["catalog_artifacts"], bool)
        or not isinstance(expected["catalog_artifacts"], int)
        or not _is_sha256(expected["catalog_revision_set_sha256"])
    ):
        raise EvalError("span manifest lacks a complete catalog binding")
    run_id, artifacts = ingestion.load_current_artifacts(catalog)
    revision_set = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda value: value.relative_path):
        revision_set.update(
            (
                artifact.relative_path
                + "\0"
                + artifact.revision_id
                + "\0"
                + artifact.content_sha256
                + "\n"
            ).encode("utf-8")
        )
    observed = {
        "catalog_run_id": run_id,
        "catalog_artifacts": len(artifacts),
        "catalog_revision_set_sha256": revision_set.hexdigest(),
    }
    if observed != expected:
        raise EvalError(
            "current catalog revision set does not match the span manifest"
        )
    return observed


def _require_derived_file(path: Path, *, label: str) -> Path:
    path = security.require_private_file(path)
    root = security.require_private_directory(
        artifact_runtime.DEFAULT_DERIVED_ROOT
    )
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EvalError(f"{label} must be under the private derived root") from exc
    return path


def _prepare_new_derived_file(path: Path, *, label: str) -> Path:
    path = path.expanduser().absolute()
    root = security.ensure_private_directory(artifact_runtime.DEFAULT_DERIVED_ROOT)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EvalError(f"{label} must be under the private derived root") from exc
    security.ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise EvalError(f"{label} already exists: {path}")
    return path


def _verified_evidence_document(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    path = _require_derived_file(path, label=label)
    raw_digest = _file_sha256(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"{label} is invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise EvalError(f"{label} must be an object")
    embedded = document.get("evidence_digest")
    unsigned = dict(document)
    unsigned.pop("evidence_digest", None)
    if not _is_sha256(embedded) or embedded != _sha256_bytes(
        _canonical_json(unsigned)
    ):
        raise EvalError(f"{label} embedded evidence digest is invalid")
    return document, raw_digest


def _validate_migration_evidence(
    path: Path,
    *,
    manifest: Path,
    manifest_sha256: str,
    span_manifest_digest: str,
    profile_digest: str,
    embedding_model_manifest_digest: str,
    collection: str,
    generation: str,
    expected_points: int,
) -> dict[str, str]:
    document, raw_digest = _verified_evidence_document(
        path,
        label="migration evidence",
    )
    manifest_record = document.get("manifest")
    qdrant_record = document.get("qdrant")
    embedding_record = document.get("embedding")
    checkpoint_record = document.get("checkpoint")
    migration_contract = document.get("migration_contract")
    migration_contract_digest = document.get("migration_contract_digest")
    point_set = (
        qdrant_record.get("point_set")
        if isinstance(qdrant_record, dict)
        else None
    )
    expected_manifest = {
        "path": str(manifest.expanduser().absolute()),
        "file_sha256": manifest_sha256,
        "span_manifest_digest": span_manifest_digest,
        "profile_digest": profile_digest,
        "model_manifest_digest": embedding_model_manifest_digest,
        "spans": expected_points,
    }
    if document.get("status") != "passed":
        raise EvalError("migration evidence is not passed")
    if not isinstance(manifest_record, dict) or any(
        manifest_record.get(key) != value
        for key, value in expected_manifest.items()
    ):
        raise EvalError("migration evidence manifest identity does not match")
    if (
        not isinstance(qdrant_record, dict)
        or qdrant_record.get("collection") != collection
        or qdrant_record.get("generation") != generation
        or qdrant_record.get("points") != expected_points
        or qdrant_record.get("content_payload") is not False
        or not isinstance(point_set, dict)
        or point_set.get("points") != expected_points
        or point_set.get("span_manifest_digest") != span_manifest_digest
        or point_set.get("payload_contract") != "exact-manifest-row-v1"
        or point_set.get("vector_contract")
        != (
            "sorted-point-id-sha256-float32le-v1/"
            f"{retrieval.EMBEDDING_DIMENSIONS}"
        )
    ):
        raise EvalError("migration evidence Qdrant identity does not match")
    vector_set_digest = point_set.get("vector_set_sha256")
    if not _is_sha256(vector_set_digest):
        raise EvalError("migration evidence lacks an exact vector-set digest")
    if (
        not isinstance(embedding_record, dict)
        or embedding_record.get("model") != EMBEDDING_MODEL
        or embedding_record.get("model_manifest_digest")
        != embedding_model_manifest_digest
        or embedding_record.get("local_files_only") is not True
    ):
        raise EvalError("migration evidence embedding identity does not match")
    execution_parameters = {
        "publication_batch_size": embedding_record.get(
            "publication_batch_size"
        ),
        "inference_batch_size": embedding_record.get(
            "inference_batch_size"
        ),
        "threads": embedding_record.get("threads"),
    }
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in execution_parameters.values()
        )
        or not isinstance(migration_contract, dict)
        or not _is_sha256(migration_contract_digest)
        or _sha256_bytes(_canonical_json(migration_contract))
        != migration_contract_digest
        or migration_contract
        != migration._migration_contract(
            batch_size=execution_parameters["publication_batch_size"],
            inference_batch_size=execution_parameters[
                "inference_batch_size"
            ],
            threads=execution_parameters["threads"],
        )
    ):
        raise EvalError(
            "migration execution contract does not match the current release"
        )
    if (
        not isinstance(checkpoint_record, dict)
        or checkpoint_record.get("points") != expected_points
        or checkpoint_record.get("vector_attestations") != expected_points
        or checkpoint_record.get("vector_set_sha256")
        != vector_set_digest
        or isinstance(checkpoint_record.get("last_row_id"), bool)
        or not isinstance(checkpoint_record.get("last_row_id"), int)
        or checkpoint_record.get("last_row_id") < expected_points
        or not isinstance(checkpoint_record.get("path"), str)
        or not Path(checkpoint_record["path"]).is_absolute()
    ):
        raise EvalError(
            "migration checkpoint/vector attestation does not match"
        )
    checkpoint_path = _require_derived_file(
        Path(checkpoint_record["path"]),
        label="migration checkpoint",
    )
    try:
        with sqlite3.connect(
            f"file:{checkpoint_path}?mode=ro",
            uri=True,
        ) as checkpoint:
            integrity = str(
                checkpoint.execute("PRAGMA quick_check").fetchone()[0]
            )
            state_metadata = {
                str(row[0]): str(row[1])
                for row in checkpoint.execute(
                    "SELECT key, value FROM metadata"
                )
            }
            progress = checkpoint.execute(
                """
                SELECT last_row_id, points
                FROM progress
                WHERE singleton=1
                """
            ).fetchone()
            vector_leaves = [
                bytes(row[0])
                for row in checkpoint.execute(
                    "SELECT leaf_sha256 FROM vector_attestations"
                )
            ]
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise EvalError(
            f"migration checkpoint is invalid: {exc}"
        ) from exc
    checkpoint_digest = hashlib.sha256()
    for leaf in sorted(vector_leaves):
        checkpoint_digest.update(leaf)
    expected_state_metadata = {
        "manifest_sha256": manifest_sha256,
        "collection": collection,
        "generation": generation,
        "model_manifest_digest": embedding_model_manifest_digest,
        "execution_contract_digest": str(migration_contract_digest),
    }
    if (
        integrity != "ok"
        or progress
        != (
            checkpoint_record["last_row_id"],
            checkpoint_record["points"],
        )
        or len(vector_leaves) != expected_points
        or checkpoint_digest.hexdigest() != vector_set_digest
        or any(
            state_metadata.get(key) != value
            for key, value in expected_state_metadata.items()
        )
    ):
        raise EvalError(
            "migration checkpoint state does not match its evidence"
        )
    if (
        document.get("canonical_mutation") != "disabled"
        or document.get("prior_generations_modified") is not False
    ):
        raise EvalError("migration evidence mutation boundary is invalid")
    return {
        "path": str(path.expanduser().absolute()),
        "file_sha256": raw_digest,
        "evidence_digest": str(document["evidence_digest"]),
        "vector_set_sha256": str(vector_set_digest),
        "migration_contract_digest": str(migration_contract_digest),
    }


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvalError(f"{label} has unknown keys: {', '.join(unknown)}")


def validate_gold(document: Any, expected_split: str | None = None) -> GoldSuite:
    if expected_split not in (None, "dev", "holdout"):
        raise EvalError("expected split must be dev or holdout")
    if not isinstance(document, dict):
        raise EvalError("gold document must be an object")
    _strict_keys(document, TOP_KEYS, "gold document")
    missing_top = sorted(TOP_KEYS - set(document))
    if missing_top:
        raise EvalError(
            "gold document is missing keys: " + ", ".join(missing_top)
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise EvalError(f"gold schema must be exactly {SCHEMA_VERSION}")
    if document.get("gold_version") != GOLD_VERSION:
        raise EvalError(f"gold version must be exactly {GOLD_VERSION}")
    split = document.get("split")
    if split not in ("dev", "holdout"):
        raise EvalError("gold split must be dev or holdout")
    if expected_split is not None and split != expected_split:
        raise EvalError(f"gold split {split!r} is not expected {expected_split!r}")
    generation = document.get("generation")
    span_digest = document.get("span_manifest_digest")
    if not isinstance(generation, str) or not generation:
        raise EvalError("gold generation is missing")
    if (
        not isinstance(span_digest, str)
        or len(span_digest) != 64
        or any(character not in "0123456789abcdef" for character in span_digest)
    ):
        raise EvalError("gold span manifest digest is invalid")
    if (
        not isinstance(document["split_methodology"], str)
        or not document["split_methodology"]
        or not isinstance(document["adjudication"], str)
        or not document["adjudication"]
    ):
        raise EvalError("gold methodology or adjudication is invalid")
    records = document.get("records")
    if not isinstance(records, list) or len(records) != EXPECTED_RECORDS_PER_SPLIT:
        raise EvalError(
            f"gold split must contain exactly {EXPECTED_RECORDS_PER_SPLIT} records"
        )

    seen: set[str] = set()
    answerable = 0
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        label = f"gold record {index}"
        if not isinstance(record, dict):
            raise EvalError(f"{label} must be an object")
        _strict_keys(record, RECORD_KEYS, label)
        required = {
            "query_id",
            "split",
            "query",
            "categories",
            "answerable",
            "hard_negative_paths",
            "allowed_alternate_paths",
            "scope",
            "adjudication",
            "judgments",
        }
        missing = sorted(required - set(record))
        if missing:
            raise EvalError(f"{label} is missing keys: {', '.join(missing)}")
        query_id = record["query_id"]
        if not isinstance(query_id, str) or not query_id.startswith(f"{split}-"):
            raise EvalError(f"{label} query_id is invalid for split {split}")
        if query_id in seen:
            raise EvalError(f"duplicate gold query_id: {query_id}")
        seen.add(query_id)
        if record["split"] != split:
            raise EvalError(f"{query_id} crosses the split boundary")
        query = record["query"]
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise EvalError(f"{query_id} query is invalid")
        categories = record["categories"]
        if (
            not isinstance(categories, list)
            or not categories
            or any(not isinstance(value, str) or not value for value in categories)
        ):
            raise EvalError(f"{query_id} categories are invalid")
        if not isinstance(record["answerable"], bool):
            raise EvalError(f"{query_id} answerable must be boolean")
        if (
            not isinstance(record["scope"], str)
            or record["scope"] != CURRENT_ONLY_SCOPE
            or not isinstance(record["adjudication"], str)
            or not record["adjudication"]
        ):
            raise EvalError(
                f"{query_id} scope must be exactly {CURRENT_ONLY_SCOPE!r} "
                "and adjudication must be present"
            )
        for field in ("hard_negative_paths", "allowed_alternate_paths"):
            value = record[field]
            if (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item for item in value)
                or len(set(value)) != len(value)
            ):
                raise EvalError(f"{query_id} {field} is invalid")
        judgments = record["judgments"]
        if not isinstance(judgments, list):
            raise EvalError(f"{query_id} judgments must be a list")
        judgment_spans: set[str] = set()
        for judgment_index, judgment in enumerate(judgments):
            if not isinstance(judgment, dict):
                raise EvalError(f"{query_id} judgment {judgment_index} is invalid")
            _strict_keys(
                judgment,
                JUDGMENT_KEYS,
                f"{query_id} judgment {judgment_index}",
            )
            missing_judgment = sorted(JUDGMENT_KEYS - set(judgment))
            if missing_judgment:
                raise EvalError(
                    f"{query_id} judgment is missing: "
                    + ", ".join(missing_judgment)
                )
            span_id = judgment["span_id"]
            if not isinstance(span_id, str) or not span_id:
                raise EvalError(f"{query_id} judgment span_id is invalid")
            if span_id in judgment_spans:
                raise EvalError(f"{query_id} repeats judgment span {span_id}")
            judgment_spans.add(span_id)
            if judgment["grade"] not in (1, 2, 3):
                raise EvalError(f"{query_id} judgment grade must be 1, 2, or 3")
            relative_path = judgment["relative_path"]
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or Path(relative_path).is_absolute()
                or ".." in Path(relative_path).parts
            ):
                raise EvalError(f"{query_id} judgment relative_path is unsafe")
            revision_id = judgment["revision_id"]
            if (
                not isinstance(revision_id, str)
                or not revision_id.startswith("revision:")
                or len(revision_id) != 73
                or any(
                    character not in "0123456789abcdef"
                    for character in revision_id.removeprefix("revision:")
                )
            ):
                raise EvalError(f"{query_id} judgment revision_id is invalid")
            if (
                not span_id.startswith("span:")
                or len(span_id) != 69
                or any(
                    character not in "0123456789abcdef"
                    for character in span_id.removeprefix("span:")
                )
            ):
                raise EvalError(f"{query_id} judgment span_id is invalid")
            if not isinstance(judgment["point_id"], str):
                raise EvalError(f"{query_id} judgment point_id is invalid")
            try:
                uuid.UUID(judgment["point_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise EvalError(
                    f"{query_id} judgment point_id is invalid"
                ) from exc
            span_sha = judgment["span_sha256"]
            if (
                not isinstance(span_sha, str)
                or len(span_sha) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in span_sha
                )
            ):
                raise EvalError(f"{query_id} judgment span hash is invalid")
            if (
                not isinstance(judgment["byte_start"], int)
                or not isinstance(judgment["byte_end"], int)
                or not 0 <= judgment["byte_start"] < judgment["byte_end"]
            ):
                raise EvalError(f"{query_id} judgment byte interval is invalid")
        if record["answerable"]:
            answerable += 1
            if not judgments:
                raise EvalError(f"{query_id} has no positive judgment")
            if (
                not isinstance(record.get("expected_relative_path"), str)
                or not isinstance(record.get("expected_revision_id"), str)
            ):
                raise EvalError(f"{query_id} lacks its expected current target")
            if not any(
                value["relative_path"] == record["expected_relative_path"]
                and value["revision_id"] == record["expected_revision_id"]
                for value in judgments
            ):
                raise EvalError(
                    f"{query_id} expected target has no exact-span judgment"
                )
        elif judgments:
            raise EvalError(f"{query_id} is unanswerable but has judgments")
        normalized.append(json.loads(json.dumps(record)))

    if answerable != EXPECTED_ANSWERABLE_PER_SPLIT:
        raise EvalError(
            f"gold split must contain {EXPECTED_ANSWERABLE_PER_SPLIT} answerable "
            f"records, observed {answerable}"
        )
    negatives = len(records) - answerable
    if negatives != EXPECTED_NEGATIVE_PER_SPLIT:
        raise EvalError(
            f"gold split must contain {EXPECTED_NEGATIVE_PER_SPLIT} negatives"
        )
    digest = _sha256_bytes(_canonical_json(document))
    return GoldSuite(
        records=tuple(normalized),
        split=split,
        gold_digest=digest,
        generation=generation,
        span_manifest_digest=span_digest,
    )


def load_gold(path: Path, expected_split: str) -> GoldSuite:
    path = path.expanduser().absolute()
    security.require_private_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"invalid gold document: {exc}") from exc
    return validate_gold(document, expected_split)


def validate_gold_manifest(
    document: Any,
    dev_digest: str | None = None,
    holdout_digest: str | None = None,
) -> dict[str, Any]:
    """Validate the query-free binding between independently stored splits."""
    if not isinstance(document, dict):
        raise EvalError("gold manifest must be an object")
    _strict_keys(document, GOLD_MANIFEST_KEYS, "gold manifest")
    missing = sorted(GOLD_MANIFEST_KEYS - set(document))
    if missing:
        raise EvalError("gold manifest is missing keys: " + ", ".join(missing))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise EvalError(f"gold manifest schema must be exactly {SCHEMA_VERSION}")
    if document.get("gold_version") != GOLD_VERSION:
        raise EvalError(f"gold manifest version must be exactly {GOLD_VERSION}")
    for key in ("generation", "split_methodology", "adjudication"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise EvalError(f"gold manifest {key} is invalid")
    span_digest = document["span_manifest_digest"]
    if (
        not isinstance(span_digest, str)
        or len(span_digest) != 64
        or any(character not in "0123456789abcdef" for character in span_digest)
    ):
        raise EvalError("gold manifest span_manifest_digest is invalid")
    splits = document.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"dev", "holdout"}:
        raise EvalError("gold manifest must bind exactly dev and holdout")
    expected_digests = {"dev": dev_digest, "holdout": holdout_digest}
    for split, value in splits.items():
        if not isinstance(value, dict):
            raise EvalError(f"gold manifest {split} entry must be an object")
        _strict_keys(
            value,
            GOLD_MANIFEST_SPLIT_KEYS,
            f"gold manifest {split}",
        )
        if set(value) != GOLD_MANIFEST_SPLIT_KEYS:
            raise EvalError(f"gold manifest {split} entry is incomplete")
        digest = value["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise EvalError(f"gold manifest {split} digest is invalid")
        if value["record_count"] != EXPECTED_RECORDS_PER_SPLIT:
            raise EvalError(f"gold manifest {split} count is invalid")
        filename = value["file"]
        if (
            not isinstance(filename, str)
            or not filename
            or filename in (".", "..")
            or Path(filename).name != filename
        ):
            raise EvalError(f"gold manifest {split} filename is unsafe")
        if expected_digests[split] is not None and digest != expected_digests[split]:
            raise EvalError(f"gold manifest {split} digest does not match")
    if splits["dev"]["file"] == splits["holdout"]["file"]:
        raise EvalError("gold manifest split filenames must be distinct")
    encoded = _canonical_json(document)
    if b'"query"' in encoded or b'"records"' in encoded or b'"judgments"' in encoded:
        raise EvalError("gold manifest must not contain queries or judgments")
    return {
        "document": json.loads(json.dumps(document)),
        "manifest_digest": _sha256_bytes(encoded),
    }


def load_gold_manifest(path: Path) -> dict[str, Any]:
    path = path.expanduser().absolute()
    security.require_private_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"invalid gold manifest: {exc}") from exc
    return validate_gold_manifest(document)


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    if not 0.0 <= percentile <= 1.0:
        raise EvalError("percentile must be between zero and one")
    ordered = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0.0 for value in ordered):
        raise EvalError("percentile values must be finite and non-negative")
    ordered.sort()
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _overlap_duplicate(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if (
        left.get("revision_id") != right.get("revision_id")
        or not isinstance(left.get("span"), dict)
        or not isinstance(right.get("span"), dict)
    ):
        return False
    left_start = int(left["span"]["byte_start"])
    left_end = int(left["span"]["byte_end"])
    right_start = int(right["span"]["byte_start"])
    right_end = int(right["span"]["byte_end"])
    intersection = max(0, min(left_end, right_end) - max(left_start, right_start))
    union = max(left_end, right_end) - min(left_start, right_start)
    return bool(union and intersection / union >= 0.80)


def score_query(
    gold_record: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    k: int = TOP_K,
) -> dict[str, Any]:
    if k < 1:
        raise EvalError("evaluation k must be positive")
    results_value = response.get("results", [])
    if not isinstance(results_value, list):
        raise EvalError("retrieval response results must be a list")
    results = results_value[:k]
    judgments = {
        str(value["span_id"]): value for value in gold_record["judgments"]
    }
    relevant_ranks: list[int] = []
    seen_relevant: set[str] = set()
    citation_correct = False
    for rank, result in enumerate(results, 1):
        result_span = str(result.get("span_id"))
        judgment = judgments.get(result_span)
        if judgment is None:
            continue
        if result_span not in seen_relevant:
            relevant_ranks.append(rank)
            seen_relevant.add(result_span)
        if (
            result.get("relative_path") == judgment["relative_path"]
            and result.get("revision_id") == judgment["revision_id"]
            and result.get("point_id") == judgment["point_id"]
            and result.get("span_sha256") == judgment["span_sha256"]
            and isinstance(result.get("span"), dict)
            and result["span"].get("byte_start") == judgment["byte_start"]
            and result["span"].get("byte_end") == judgment["byte_end"]
            and result.get("source_sha256_verified") is True
            and result.get("span_sha256_verified") is True
            and result.get("manifest_current") is True
        ):
            citation_correct = True
    reciprocal_rank = 1.0 / min(relevant_ranks) if relevant_ranks else 0.0
    gain_by_span = {
        str(value["span_id"]): int(value["grade"])
        for value in gold_record["judgments"]
    }
    dcg = 0.0
    credited: set[str] = set()
    for rank, result in enumerate(results, 1):
        span_id = str(result.get("span_id"))
        gain = gain_by_span.get(span_id, 0)
        if gain <= 0 or span_id in credited:
            continue
        credited.add(span_id)
        dcg += (2**gain - 1) / math.log2(rank + 1)
    ideal = sorted(gain_by_span.values(), reverse=True)[:k]
    idcg = sum(
        (2**gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(ideal, 1)
    )

    duplicate_slots = 0
    for right_index, right in enumerate(results):
        for left in results[:right_index]:
            same_copy = (
                left.get("content_sha256") == right.get("content_sha256")
                and left.get("span_sha256") == right.get("span_sha256")
                and left.get("span") == right.get("span")
            )
            if same_copy or _overlap_duplicate(left, right):
                duplicate_slots += 1
                break
    hard_negatives = set(gold_record["hard_negative_paths"])
    hard_negative_count = sum(
        1 for result in results if result.get("relative_path") in hard_negatives
    )
    hash_slots = len(results)
    verified_hash_slots = sum(
        1
        for result in results
        if result.get("source_sha256_verified") is True
        and result.get("span_sha256_verified") is True
    )
    historical_target = (
        None
        if gold_record["answerable"]
        else gold_record.get("expected_revision_id")
    )
    historical_target_leaks = sum(
        1
        for result in results
        if historical_target is not None
        and result.get("revision_id") == historical_target
    )
    noncurrent_or_historical_slots = sum(
        1
        for result in results
        if result.get("manifest_current") is not True
        or (
            historical_target is not None
            and result.get("revision_id") == historical_target
        )
    )
    abstained = response.get("abstained") is True
    answerable = bool(gold_record["answerable"])
    return {
        "query_id": gold_record["query_id"],
        "answerable": answerable,
        "abstained": abstained,
        "classification_correct": (not abstained) if answerable else abstained,
        "recall_at_k": 1.0 if relevant_ranks else 0.0,
        "reciprocal_rank_at_k": reciprocal_rank,
        "ndcg_at_k": dcg / idcg if idcg else 0.0,
        "citation_correct": citation_correct if answerable else None,
        "hash_slots": hash_slots,
        "verified_hash_slots": verified_hash_slots,
        "noncurrent_or_historical_slots": noncurrent_or_historical_slots,
        "historical_target_leaks": historical_target_leaks,
        "hard_negative_slots": hard_negative_count,
        "duplicate_slots": duplicate_slots,
        "result_count": len(results),
        "verification_failure_count": len(
            response.get("verification_failures", [])
            if isinstance(response.get("verification_failures", []), list)
            else []
        ),
        "result_span_ids": [str(value.get("span_id")) for value in results],
        "result_paths": [str(value.get("relative_path")) for value in results],
        "abstention_features": response.get("abstention_features"),
        "abstention_reason": response.get("abstention_reason"),
    }


def aggregate_metrics(
    gold_records: Sequence[Mapping[str, Any]],
    responses_by_id: Mapping[str, Mapping[str, Any]],
    latencies_ms: Sequence[float] | Mapping[str, float],
    *,
    k: int = TOP_K,
) -> dict[str, Any]:
    expected_ids = {str(record["query_id"]) for record in gold_records}
    if set(responses_by_id) != expected_ids:
        missing = sorted(expected_ids - set(responses_by_id))
        extra = sorted(set(responses_by_id) - expected_ids)
        raise EvalError(f"response ID mismatch; missing={missing}, extra={extra}")
    scores = [
        score_query(record, responses_by_id[str(record["query_id"])], k=k)
        for record in gold_records
    ]
    positives = [value for value in scores if value["answerable"]]
    negatives = [value for value in scores if not value["answerable"]]
    if isinstance(latencies_ms, Mapping):
        if set(latencies_ms) != expected_ids:
            raise EvalError("latency IDs do not match the gold records")
        latency_values = [
            float(latencies_ms[str(record["query_id"])])
            for record in gold_records
        ]
    else:
        latency_values = [float(value) for value in latencies_ms]
        if len(latency_values) != len(gold_records):
            raise EvalError("latency count does not match the gold records")
    if any(
        not math.isfinite(value) or value < 0.0 for value in latency_values
    ):
        raise EvalError("latencies must be finite and non-negative")
    hash_slots = sum(value["hash_slots"] for value in scores)
    warm_latency_values = latency_values[1:] or latency_values
    category_metrics: dict[str, Any] = {}
    categories = sorted(
        {
            str(category)
            for record in gold_records
            for category in record.get("categories", [])
        }
    )
    score_by_id = {str(value["query_id"]): value for value in scores}
    for category in categories:
        category_scores = [
            score_by_id[str(record["query_id"])]
            for record in gold_records
            if category in record.get("categories", [])
        ]
        category_positives = [
            value for value in category_scores if value["answerable"]
        ]
        category_negatives = [
            value for value in category_scores if not value["answerable"]
        ]
        category_metrics[category] = {
            "queries": len(category_scores),
            "answerable_queries": len(category_positives),
            "negative_queries": len(category_negatives),
            f"recall_at_{k}": (
                statistics.fmean(
                    value["recall_at_k"] for value in category_positives
                )
                if category_positives
                else None
            ),
            f"mrr_at_{k}": (
                statistics.fmean(
                    value["reciprocal_rank_at_k"]
                    for value in category_positives
                )
                if category_positives
                else None
            ),
            f"ndcg_at_{k}": (
                statistics.fmean(
                    value["ndcg_at_k"] for value in category_positives
                )
                if category_positives
                else None
            ),
            "negative_abstention_accuracy": (
                statistics.fmean(
                    1.0 if value["classification_correct"] else 0.0
                    for value in category_negatives
                )
                if category_negatives
                else None
            ),
            "noncurrent_or_historical_slots": sum(
                value["noncurrent_or_historical_slots"]
                for value in category_scores
            ),
            "duplicate_slots": sum(
                value["duplicate_slots"] for value in category_scores
            ),
        }
    return {
        "queries": len(scores),
        "answerable_queries": len(positives),
        "negative_queries": len(negatives),
        f"recall_at_{k}": statistics.fmean(
            value["recall_at_k"] for value in positives
        ),
        f"mrr_at_{k}": statistics.fmean(
            value["reciprocal_rank_at_k"] for value in positives
        ),
        f"ndcg_at_{k}": statistics.fmean(
            value["ndcg_at_k"] for value in positives
        ),
        "exact_citation_accuracy": statistics.fmean(
            1.0 if value["citation_correct"] else 0.0 for value in positives
        ),
        "answerable_acceptance_accuracy": statistics.fmean(
            1.0 if value["classification_correct"] else 0.0 for value in positives
        ),
        "negative_abstention_accuracy": statistics.fmean(
            1.0 if value["classification_correct"] else 0.0 for value in negatives
        ),
        "overall_classification_accuracy": statistics.fmean(
            1.0 if value["classification_correct"] else 0.0 for value in scores
        ),
        "hash_verification_rate": (
            sum(value["verified_hash_slots"] for value in scores) / hash_slots
            if hash_slots
            else 1.0
        ),
        "noncurrent_or_historical_slots": sum(
            value["noncurrent_or_historical_slots"] for value in scores
        ),
        "historical_target_leaks": sum(
            value["historical_target_leaks"] for value in scores
        ),
        "hard_negative_slots": sum(
            value["hard_negative_slots"] for value in scores
        ),
        "duplicate_slots": sum(value["duplicate_slots"] for value in scores),
        "underfilled_queries": sum(
            1
            for value in scores
            if not value["abstained"] and value["result_count"] < k
        ),
        "verification_failures": sum(
            value["verification_failure_count"] for value in scores
        ),
        "latency_ms": {
            "min": min(latency_values),
            "p50": nearest_rank_percentile(latency_values, 0.50),
            "median": statistics.median(latency_values),
            "p95": nearest_rank_percentile(latency_values, 0.95),
            "p99": nearest_rank_percentile(latency_values, 0.99),
            "max": max(latency_values),
            "warm_p50": nearest_rank_percentile(warm_latency_values, 0.50),
            "warm_p95": nearest_rank_percentile(warm_latency_values, 0.95),
        },
        "categories": category_metrics,
        "per_query": scores,
    }


def _feature_thresholds(
    responses: Mapping[str, Mapping[str, Any]],
    field: str,
) -> list[float]:
    values = sorted(
        {
            round(float(features[field]), 6)
            for response in responses.values()
            if isinstance((features := response.get("abstention_features")), dict)
            and features.get(field) is not None
        }
    )
    if not values:
        return [0.0]
    indexes = {
        0,
        len(values) - 1,
        *(
            min(len(values) - 1, math.floor(fraction * (len(values) - 1)))
            for fraction in (0.1, 0.25, 0.5, 0.75, 0.9)
        ),
    }
    selected = {values[index] for index in indexes}
    selected.add(math.nextafter(values[0], -math.inf))
    selected.add(math.nextafter(values[-1], math.inf))
    return sorted(selected)


def _apply_policy(
    responses: Mapping[str, Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    adjusted: dict[str, dict[str, Any]] = {}
    for query_id, original in responses.items():
        response = json.loads(json.dumps(original))
        features = response.get("abstention_features")
        if not isinstance(features, dict):
            raise EvalError(f"{query_id} response lacks abstention features")
        abstained, reason = retrieval.should_abstain(features, dict(policy))
        response["abstained"] = abstained
        response["abstention_reason"] = reason
        if abstained:
            response["results"] = []
        adjusted[query_id] = response
    return adjusted


def select_abstention_policy(
    dev_records: Sequence[Mapping[str, Any]],
    dev_responses: Mapping[str, Mapping[str, Any]],
    candidate_policies: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if any(record.get("split") != "dev" for record in dev_records):
        raise EvalError("abstention tuning accepts development records only")
    if candidate_policies is None:
        cross = _feature_thresholds(dev_responses, "cross_score")
        margins = _feature_thresholds(dev_responses, "cross_margin")
        generated: list[dict[str, float]] = []
        for exact_min in cross:
            for agreement_min in cross:
                for single_min in cross:
                    if not exact_min <= agreement_min <= single_min:
                        continue
                    for margin_min in margins:
                        generated.append(
                            {
                                "exact_identifier_cross_min": exact_min,
                                "agreement_cross_min": agreement_min,
                                "single_channel_cross_min": single_min,
                                "cross_margin_min": margin_min,
                            }
                        )
        candidate_policies = generated
    best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    for raw_policy in candidate_policies:
        policy = {
            key: float(raw_policy[key])
            for key in (
                "exact_identifier_cross_min",
                "agreement_cross_min",
                "single_channel_cross_min",
                "cross_margin_min",
            )
        }
        adjusted = _apply_policy(dev_responses, policy)
        metrics = aggregate_metrics(
            dev_records,
            adjusted,
            [0.0] * len(dev_records),
        )
        feasible = (
            metrics["negative_abstention_accuracy"] >= 0.95
            and metrics[f"recall_at_{TOP_K}"] >= 0.90
        )
        objective = (
            1 if feasible else 0,
            metrics["negative_abstention_accuracy"],
            metrics[f"recall_at_{TOP_K}"],
            metrics["overall_classification_accuracy"],
            metrics[f"mrr_at_{TOP_K}"],
            metrics[f"ndcg_at_{TOP_K}"],
            policy["single_channel_cross_min"],
            policy["agreement_cross_min"],
            policy["exact_identifier_cross_min"],
            policy["cross_margin_min"],
        )
        if best is None or objective > best[0]:
            best = (
                objective,
                {
                    "policy": policy,
                    "feasible": feasible,
                    "development_metrics": {
                        key: value
                        for key, value in metrics.items()
                        if key != "per_query"
                    },
                },
            )
    if best is None:
        raise EvalError("no candidate abstention policies were supplied")
    return best[1]


def freeze_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "exact_identifier_cross_min",
        "agreement_cross_min",
        "single_channel_cross_min",
        "cross_margin_min",
    }
    if set(policy) != expected:
        raise EvalError("abstention policy keys do not match the frozen schema")
    normalized = {key: float(policy[key]) for key in sorted(expected)}
    if any(not math.isfinite(value) for value in normalized.values()):
        raise EvalError("abstention policy values must be finite")
    return {
        "schema_version": SCHEMA_VERSION,
        "ranking_version": retrieval.RANKING_VERSION,
        "policy": normalized,
        "policy_digest": _sha256_bytes(_canonical_json(normalized)),
    }


def _holdout_ledger_path(
    *,
    gold_file_sha256: str,
    generation: str,
    span_manifest_digest: str,
) -> Path:
    if (
        not _is_sha256(gold_file_sha256)
        or not isinstance(generation, str)
        or not generation
        or not _is_sha256(span_manifest_digest)
    ):
        raise EvalError("holdout ledger identity is invalid")
    identity = {
        "schema_version": SCHEMA_VERSION,
        "gold_file_sha256": gold_file_sha256,
        "generation": generation,
        "span_manifest_digest": span_manifest_digest,
    }
    identity_digest = _sha256_bytes(_canonical_json(identity))
    return (
        artifact_runtime.DEFAULT_DERIVED_ROOT
        / f"artifact-retrieval-holdout-{identity_digest}.json"
    ).expanduser().absolute()


def _reserve_holdout(
    *,
    gold_file_sha256: str,
    generation: str,
    span_manifest_digest: str,
    policy_digest: str,
    system_digest: str,
) -> tuple[Path, dict[str, Any]]:
    path = _holdout_ledger_path(
        gold_file_sha256=gold_file_sha256,
        generation=generation,
        span_manifest_digest=span_manifest_digest,
    )
    directory = security.ensure_private_directory(path.parent)
    reservation = {
        "schema_version": SCHEMA_VERSION,
        "state": "reserved",
        "reserved_unix": time.time(),
        "gold_file_sha256": gold_file_sha256,
        "generation": generation,
        "span_manifest_digest": span_manifest_digest,
        "policy_digest": policy_digest,
        "system_digest": system_digest,
    }
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise EvalError(
            "the frozen holdout has already been reserved or evaluated"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical_json(reservation) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    security.secure_created_file(path)
    security.fsync_directory(directory)
    return path, reservation


def record_holdout_run(
    *,
    gold_digest: str,
    gold_file_sha256: str,
    generation: str,
    span_manifest_digest: str,
    policy_digest: str,
    system_digest: str,
    evidence_digest: str,
    evidence_file_sha256: str,
) -> dict[str, Any]:
    path = _holdout_ledger_path(
        gold_file_sha256=gold_file_sha256,
        generation=generation,
        span_manifest_digest=span_manifest_digest,
    )
    security.require_private_file(path)
    existing = json.loads(path.read_text(encoding="utf-8"))
    if existing.get("state") != "reserved":
        raise EvalError("holdout ledger is not in the reserved state")
    expected = {
        "gold_file_sha256": gold_file_sha256,
        "generation": generation,
        "span_manifest_digest": span_manifest_digest,
        "policy_digest": policy_digest,
        "system_digest": system_digest,
    }
    for key, value in expected.items():
        if existing.get(key) != value:
            raise EvalError(f"holdout ledger {key} does not match the run")
    completed = {
        **existing,
        "state": "completed",
        "completed_unix": time.time(),
        "gold_digest": gold_digest,
        "evidence_digest": evidence_digest,
        "evidence_file_sha256": evidence_file_sha256,
    }
    security.atomic_write_json(path, completed, replace=True)
    return completed


def validate_completed_holdout_ledger(
    *,
    gold_file_sha256: str,
    generation: str,
    span_manifest_digest: str,
    policy_digest: str,
    system_digest: str,
    gold_digest: str,
    evidence_digest: str,
    evidence_file_sha256: str,
) -> dict[str, Any]:
    path = _holdout_ledger_path(
        gold_file_sha256=gold_file_sha256,
        generation=generation,
        span_manifest_digest=span_manifest_digest,
    )
    path = _require_derived_file(path, label="completed holdout ledger")
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"completed holdout ledger is invalid: {exc}") from exc
    expected_keys = {
        "schema_version",
        "state",
        "reserved_unix",
        "completed_unix",
        "gold_file_sha256",
        "generation",
        "span_manifest_digest",
        "policy_digest",
        "system_digest",
        "gold_digest",
        "evidence_digest",
        "evidence_file_sha256",
    }
    expected = {
        "schema_version": SCHEMA_VERSION,
        "state": "completed",
        "gold_file_sha256": gold_file_sha256,
        "generation": generation,
        "span_manifest_digest": span_manifest_digest,
        "policy_digest": policy_digest,
        "system_digest": system_digest,
        "gold_digest": gold_digest,
        "evidence_digest": evidence_digest,
        "evidence_file_sha256": evidence_file_sha256,
    }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != expected_keys
        or any(ledger.get(key) != value for key, value in expected.items())
        or isinstance(ledger.get("reserved_unix"), bool)
        or not isinstance(ledger.get("reserved_unix"), (int, float))
        or not math.isfinite(float(ledger["reserved_unix"]))
        or isinstance(ledger.get("completed_unix"), bool)
        or not isinstance(ledger.get("completed_unix"), (int, float))
        or not math.isfinite(float(ledger["completed_unix"]))
        or float(ledger["completed_unix"])
        < float(ledger["reserved_unix"])
    ):
        raise EvalError(
            "completed holdout ledger does not match the frozen release"
        )
    return ledger


def _manifest_metadata(path: Path) -> tuple[dict[str, Any], str]:
    security.require_private_file(path)
    with sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1",
        uri=True,
    ) as connection:
        metadata = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute(
                "SELECT key, value_json FROM metadata ORDER BY key"
            )
        }
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if version != retrieval.SCHEMA_VERSION or integrity != "ok":
        raise EvalError("span manifest schema or integrity check failed")
    return metadata, _file_sha256(path)


def _build_system_identity(
    *,
    gold_binding: Mapping[str, Any],
    metadata: Mapping[str, Any],
    manifest_sha256: str,
    collection: str,
    embedding_digest: str,
    reranker_digest: str,
    migration_evidence: Mapping[str, str],
    catalog_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    ranking_contract = retrieval.ranking_contract()
    system = {
        "generation": gold_binding["generation"],
        "span_manifest_digest": gold_binding["span_manifest_digest"],
        "span_manifest_file_sha256": manifest_sha256,
        "collection": collection,
        "profile_digest": metadata["profile_digest"],
        "embedding_model": EMBEDDING_MODEL,
        "embedding_model_manifest_digest": embedding_digest,
        "reranker_model": RERANKER_MODEL,
        "reranker_model_manifest_digest": reranker_digest,
        "ranking_version": retrieval.RANKING_VERSION,
        "gold_manifest_digest": _sha256_bytes(_canonical_json(gold_binding)),
        "gold_split_file_sha256": {
            name: value["sha256"]
            for name, value in gold_binding["splits"].items()
        },
        "retrieval_scope": {
            "name": CURRENT_ONLY_SCOPE,
            "filters": dict(CURRENT_ONLY_FILTERS),
        },
        "migration_evidence": dict(migration_evidence),
        "catalog_binding": dict(catalog_binding),
        "material_sources": _material_source_manifest(),
        "evaluation_runtime_contract": _evaluation_runtime_contract(),
        "ranking_contract": ranking_contract,
    }
    return system, _sha256_bytes(_canonical_json(system))


def _validate_suite_identity(
    suite: GoldSuite,
    *,
    gold_binding: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    if (
        metadata.get("generation") != suite.generation
        or metadata.get("span_manifest_digest") != suite.span_manifest_digest
        or gold_binding.get("generation") != suite.generation
        or gold_binding.get("span_manifest_digest") != suite.span_manifest_digest
    ):
        raise EvalError("gold set is not bound to this span manifest")


def _load_release_policy(
    path: Path,
    *,
    system: Mapping[str, Any],
    system_digest: str,
) -> dict[str, Any]:
    path = _require_derived_file(path, label="frozen development policy")
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"frozen development policy is invalid: {exc}") from exc
    if not isinstance(frozen, dict) or set(frozen) != RELEASE_POLICY_KEYS:
        raise EvalError("frozen development policy schema does not match")
    if (
        frozen.get("schema_version") != SCHEMA_VERSION
        or frozen.get("ranking_version") != retrieval.RANKING_VERSION
        or frozen.get("system") != system
        or frozen.get("system_digest") != system_digest
        or _sha256_bytes(_canonical_json(frozen.get("system"))) != system_digest
        or frozen.get("development_selection_feasible") is not True
        or not _is_sha256(frozen.get("development_gold_digest"))
        or not isinstance(frozen.get("policy"), dict)
    ):
        raise EvalError("frozen development policy does not match this system")
    normalized = freeze_policy(frozen.get("policy"))
    if (
        normalized["policy"] != frozen.get("policy")
        or normalized["policy_digest"] != frozen.get("policy_digest")
        or normalized["ranking_version"] != frozen.get("ranking_version")
    ):
        raise EvalError("frozen policy digest or thresholds are invalid")
    binding = frozen.get("development_evidence")
    if (
        not isinstance(binding, dict)
        or set(binding) != DEVELOPMENT_EVIDENCE_BINDING_KEYS
        or not _is_sha256(binding.get("file_sha256"))
        or not _is_sha256(binding.get("evidence_digest"))
        or not isinstance(binding.get("path"), str)
        or not Path(binding["path"]).is_absolute()
    ):
        raise EvalError("development evidence binding is invalid")
    evidence_path = _require_derived_file(
        Path(binding["path"]),
        label="development evidence",
    )
    evidence, raw_digest = _verified_evidence_document(
        evidence_path,
        label="development evidence",
    )
    if raw_digest != binding["file_sha256"]:
        raise EvalError("development evidence file digest does not match policy")
    expected_evidence_policy = {
        key: value
        for key, value in frozen.items()
        if key != "development_evidence"
    }
    if (
        evidence.get("evidence_digest") != binding["evidence_digest"]
        or evidence.get("split") != "dev"
        or evidence.get("status") != "passed"
        or evidence.get("system") != system
        or evidence.get("system_digest") != system_digest
        or evidence.get("gold", {}).get("digest")
        != frozen["development_gold_digest"]
        or evidence.get("gate", {}).get("status") != "passed"
        or evidence.get("policy") != expected_evidence_policy
    ):
        raise EvalError("development evidence is not a passed matching release")
    return frozen


def _validate_gold_judgments(
    manifest: Path,
    suite: GoldSuite,
) -> None:
    with sqlite3.connect(
        f"file:{manifest.resolve()}?mode=ro&immutable=1",
        uri=True,
    ) as connection:
        connection.row_factory = sqlite3.Row
        for record in suite.records:
            for judgment in record["judgments"]:
                row = connection.execute(
                    """
                    SELECT *
                    FROM spans
                    WHERE span_id=? AND point_id=?
                      AND ready=1 AND catalog_current=1
                    """,
                    (judgment["span_id"], judgment["point_id"]),
                ).fetchone()
                if row is None:
                    raise EvalError(
                        f"{record['query_id']} judgment is absent from the manifest"
                    )
                expected = {
                    "relative_path": judgment["relative_path"],
                    "revision_id": judgment["revision_id"],
                    "byte_start": judgment["byte_start"],
                    "byte_end": judgment["byte_end"],
                    "span_sha256": judgment["span_sha256"],
                }
                for key, value in expected.items():
                    if row[key] != value:
                        raise EvalError(
                            f"{record['query_id']} judgment {key} mismatch"
                        )


def _annotate_current(
    manifest: Path,
    response: dict[str, Any],
) -> dict[str, Any]:
    point_ids = [
        str(result.get("point_id"))
        for result in response.get("results", [])
        if result.get("point_id") is not None
    ]
    current: set[str] = set()
    if point_ids:
        with sqlite3.connect(
            f"file:{manifest.resolve()}?mode=ro&immutable=1",
            uri=True,
        ) as connection:
            for start in range(0, len(point_ids), 500):
                batch = point_ids[start : start + 500]
                rows = connection.execute(
                    "SELECT point_id FROM spans "
                    "WHERE ready=1 AND catalog_current=1 AND point_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                )
                current.update(str(row[0]) for row in rows)
    for result in response.get("results", []):
        result["manifest_current"] = str(result.get("point_id")) in current
    return response


def _compact_response(response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "abstained": response.get("abstained"),
        "abstention_reason": response.get("abstention_reason"),
        "abstention_features": response.get("abstention_features"),
        "candidate_counts": response.get("candidate_counts"),
        "verification_failures": response.get("verification_failures"),
        "results": [
            {
                key: result.get(key)
                for key in (
                    "point_id",
                    "span_id",
                    "logical_artifact_id",
                    "revision_id",
                    "relative_path",
                    "content_sha256",
                    "span_sha256",
                    "span",
                    "source_sha256_verified",
                    "span_sha256_verified",
                    "manifest_current",
                    "alternate_provenance",
                    "ranking",
                )
            }
            for result in response.get("results", [])
        ],
    }


def _run_modes(
    *,
    suite: GoldSuite,
    manifest: Path,
    runtime: artifact_runtime.ArtifactRuntime,
    collection: str,
    embedding_snapshot: Path,
    reranker_snapshot: Path,
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, float]],
    dict[str, Any],
]:
    try:
        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise EvalError("local retrieval evaluation dependencies are missing") from exc
    lexical = retrieval.LexicalIndex(
        manifest,
        generation=suite.generation,
    )
    retriever = retrieval.HybridRetriever(
        workspace=runtime.workspace,
        catalog=runtime.catalog,
        lexical=lexical,
    )
    embedder = TextEmbedding(
        model_name=EMBEDDING_MODEL,
        specific_model_path=str(embedding_snapshot),
        local_files_only=True,
        threads=retrieval.MODEL_THREADS,
    )
    cross_encoder = TextCrossEncoder(
        RERANKER_MODEL,
        specific_model_path=str(reranker_snapshot),
        local_files_only=True,
        threads=retrieval.MODEL_THREADS,
    )
    client = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_read_key(),
        timeout=30,
    )
    responses: dict[str, dict[str, dict[str, Any]]] = {
        mode: {} for mode in MODES
    }
    latencies: dict[str, dict[str, float]] = {mode: {} for mode in MODES}

    def rerank(query: str, documents: Sequence[str]) -> Sequence[float]:
        return list(
            cross_encoder.rerank(
                query,
                documents,
                batch_size=retrieval.RERANK_BATCH_SIZE,
            )
        )

    try:
        collection_info = client.get_collection(collection)
        collection_metadata = dict(collection_info.config.metadata or {})
        for record in suite.records:
            if record.get("scope") != CURRENT_ONLY_SCOPE:
                raise EvalError("retrieval evaluation record escaped current-only scope")
            query_id = str(record["query_id"])
            query = str(record["query"])
            embedding_started = time.perf_counter()
            vector = next(iter(embedder.query_embed(query)))
            embedding_ms = (time.perf_counter() - embedding_started) * 1000.0
            for mode in MODES:
                started = time.perf_counter()
                response = retriever.search(
                    client=client,
                    collection=collection,
                    query=query,
                    query_vector=vector.tolist(),
                    limit=TOP_K,
                    mode=mode,
                    reranker=rerank if mode == "hybrid-rerank" else None,
                    abstention_policy=None,
                    **CURRENT_ONLY_FILTERS,
                )
                latencies[mode][query_id] = (
                    time.perf_counter() - started
                ) * 1000.0 + (
                    embedding_ms
                    if mode in ("vector", "hybrid", "hybrid-rerank")
                    else 0.0
                )
                responses[mode][query_id] = _annotate_current(
                    manifest,
                    response,
                )
    finally:
        client.close()
        lexical.close()
    return responses, latencies, collection_metadata


def _preflight_collection(
    *,
    runtime: artifact_runtime.ArtifactRuntime,
    collection: str,
    manifest: Path,
    expected_metadata: Mapping[str, Any],
    expected_points: int,
    expected_vector_set_sha256: str,
) -> dict[str, Any]:
    """Reject an incomplete or differently configured collection before evaluation."""
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise EvalError("Qdrant client is unavailable") from exc
    client = QdrantClient(
        url=runtime.qdrant_url,
        api_key=runtime.qdrant_read_key(),
        timeout=30,
    )
    try:
        server_version = str(client.info().version)
        if server_version != retrieval.QDRANT_SERVER_VERSION:
            raise EvalError(
                "Qdrant server version does not match the evaluated contract"
            )
        information = client.get_collection(collection)
        metadata = dict(information.config.metadata or {})
        vector_space = information.config.params.vectors
        distance = getattr(vector_space, "distance", None)
        distance_value = getattr(distance, "value", distance)
        vector_size = getattr(vector_space, "size", None)
        normalized_distance = str(distance_value).casefold()
        if (
            isinstance(vector_space, dict)
            or vector_size != retrieval.EMBEDDING_DIMENSIONS
            or normalized_distance != "cosine"
        ):
            raise EvalError(
                "Qdrant collection vector space is not "
                f"{retrieval.EMBEDDING_DIMENSIONS}-dimensional cosine"
            )
        for key, value in expected_metadata.items():
            if metadata.get(key) != value:
                raise EvalError(f"Qdrant collection metadata {key} mismatch")
        observed_points = int(
            client.count(collection_name=collection, exact=True).count
        )
        if observed_points != expected_points:
            raise EvalError(
                "Qdrant collection point count does not match the manifest: "
                f"{observed_points} != {expected_points}"
            )
        point_set = retrieval.verify_qdrant_point_set(
            client=client,
            collection=collection,
            manifest=manifest,
        )
        if (
            not isinstance(point_set, dict)
            or point_set.get("points") != expected_points
            or point_set.get("vector_set_sha256")
            != expected_vector_set_sha256
        ):
            raise EvalError(
                "Qdrant collection vector set does not match migration evidence"
            )
        return {
            "vector_size": int(vector_size),
            "distance": "Cosine",
            "points": observed_points,
            "server_version": server_version,
            "point_set": point_set,
            "metadata": metadata,
        }
    finally:
        client.close()


def _gate(
    final_metrics: Mapping[str, Any],
    baselines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    best_mrr = max(float(value[f"mrr_at_{TOP_K}"]) for value in baselines)
    best_ndcg = max(float(value[f"ndcg_at_{TOP_K}"]) for value in baselines)
    # Each bar is declared here once and travels into the artifact with its
    # observed value and verdict (F-16), so the evidence is readable without
    # the ADR that used to hold the acceptance rationale.
    summary = evidence_schema.summarize(
        [
            evidence_schema.check(
                f"recall_at_{TOP_K}",
                observed=float(final_metrics[f"recall_at_{TOP_K}"]),
                operator=">=",
                threshold=0.90,
            ),
            evidence_schema.check(
                f"mrr_at_{TOP_K}_vs_baseline",
                observed=float(final_metrics[f"mrr_at_{TOP_K}"]),
                operator=">=",
                threshold=0.98 * best_mrr,
                basis={
                    "expression": f"0.98 * best_simple_baseline.mrr_at_{TOP_K}",
                    "factor": 0.98,
                    "reference": best_mrr,
                },
            ),
            evidence_schema.check(
                f"ndcg_at_{TOP_K}_vs_baseline",
                observed=float(final_metrics[f"ndcg_at_{TOP_K}"]),
                operator=">=",
                threshold=0.98 * best_ndcg,
                basis={
                    "expression": f"0.98 * best_simple_baseline.ndcg_at_{TOP_K}",
                    "factor": 0.98,
                    "reference": best_ndcg,
                },
            ),
            evidence_schema.check(
                "exact_citation_accuracy",
                observed=float(final_metrics["exact_citation_accuracy"]),
                operator=">=",
                threshold=0.98,
            ),
            evidence_schema.check(
                "hash_verification_rate",
                observed=float(final_metrics["hash_verification_rate"]),
                operator="==",
                threshold=1.0,
            ),
            evidence_schema.check(
                "negative_abstention_accuracy",
                observed=float(final_metrics["negative_abstention_accuracy"]),
                operator=">=",
                threshold=0.95,
            ),
            evidence_schema.check(
                "no_noncurrent_or_historical_slots",
                observed=int(final_metrics["noncurrent_or_historical_slots"]),
                operator="==",
                threshold=0,
            ),
            evidence_schema.check(
                "no_historical_target_leaks",
                observed=int(final_metrics["historical_target_leaks"]),
                operator="==",
                threshold=0,
            ),
            evidence_schema.check(
                "no_hard_negative_slots",
                observed=int(final_metrics["hard_negative_slots"]),
                operator="==",
                threshold=0,
            ),
            evidence_schema.check(
                "no_duplicate_slots",
                observed=int(final_metrics["duplicate_slots"]),
                operator="==",
                threshold=0,
            ),
            evidence_schema.check(
                "no_verification_failures",
                observed=int(final_metrics["verification_failures"]),
                operator="==",
                threshold=0,
            ),
        ]
    )
    return {
        "status": summary["status"],
        # Flat booleans stay for existing readers; the enriched records beside
        # them are the format future gates should be read from.
        "checks": evidence_schema.legacy_boolean_map(summary),
        "evidence_schema_version": summary["evidence_schema_version"],
        "checks_total": summary["checks_total"],
        "checks_failed": summary["checks_failed"],
        "check_records": summary["checks"],
        "best_simple_baseline": {
            f"mrr_at_{TOP_K}": best_mrr,
            f"ndcg_at_{TOP_K}": best_ndcg,
        },
    }


def evaluate(
    *,
    split: str,
    gold_path: Path,
    gold_manifest_path: Path,
    manifest: Path,
    collection: str,
    embedding_snapshot: Path,
    reranker_snapshot: Path,
    migration_evidence_path: Path,
    policy_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    if split not in ("dev", "holdout"):
        raise EvalError("evaluation split must be dev or holdout")
    gold_path = security.require_private_file(gold_path)
    gold_binding = load_gold_manifest(gold_manifest_path)
    binding_document = gold_binding["document"]
    split_binding = binding_document["splits"][split]
    if Path(split_binding["file"]).name != gold_path.name:
        raise EvalError("gold split filename does not match the gold manifest")
    output_path = _prepare_new_derived_file(
        output_path,
        label=f"{split} evaluation evidence",
    )
    suite: GoldSuite | None = None
    if split == "dev":
        policy_path = _prepare_new_derived_file(
            policy_path,
            label="frozen development policy",
        )
        if split_binding["sha256"] != _file_sha256(gold_path):
            raise EvalError("gold split bytes do not match the gold manifest")
        suite = load_gold(gold_path, split)

    metadata, manifest_sha = _manifest_metadata(manifest)
    if (
        metadata.get("generation") != binding_document.get("generation")
        or metadata.get("span_manifest_digest")
        != binding_document.get("span_manifest_digest")
    ):
        raise EvalError("gold binding is not bound to this span manifest")
    if suite is not None:
        _validate_suite_identity(
            suite,
            gold_binding=binding_document,
            metadata=metadata,
        )
        _validate_gold_judgments(manifest, suite)

    embedding_files, embedding_digest = _path_manifest(embedding_snapshot)
    reranker_files, reranker_digest = _path_manifest(reranker_snapshot)
    if metadata.get("embedding_model_manifest_digest") != embedding_digest:
        raise EvalError("embedding snapshot does not match the span manifest")
    expected_points = int(metadata["spans"])
    migration_binding = _validate_migration_evidence(
        migration_evidence_path,
        manifest=manifest,
        manifest_sha256=manifest_sha,
        span_manifest_digest=str(binding_document["span_manifest_digest"]),
        profile_digest=str(metadata["profile_digest"]),
        embedding_model_manifest_digest=embedding_digest,
        collection=collection,
        generation=str(binding_document["generation"]),
        expected_points=expected_points,
    )
    runtime = artifact_runtime.load_runtime()
    catalog_binding = _catalog_release_binding(runtime.catalog, metadata)
    system, system_digest = _build_system_identity(
        gold_binding=binding_document,
        metadata=metadata,
        manifest_sha256=manifest_sha,
        collection=collection,
        embedding_digest=embedding_digest,
        reranker_digest=reranker_digest,
        migration_evidence=migration_binding,
        catalog_binding=catalog_binding,
    )
    expected_collection = {
        "generation": binding_document["generation"],
        "profile_digest": metadata["profile_digest"],
        "span_manifest_digest": binding_document["span_manifest_digest"],
        "manifest_file_sha256": manifest_sha,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_model_manifest_digest": embedding_digest,
        "content_payload": False,
        "current_only": True,
    }
    collection_contract = _preflight_collection(
        runtime=runtime,
        collection=collection,
        manifest=manifest,
        expected_metadata=expected_collection,
        expected_points=expected_points,
        expected_vector_set_sha256=migration_binding["vector_set_sha256"],
    )

    frozen: dict[str, Any] | None
    ledger_path: Path | None = None
    if split == "holdout":
        frozen = _load_release_policy(
            policy_path,
            system=system,
            system_digest=system_digest,
        )
        ledger_path, _reservation = _reserve_holdout(
            gold_file_sha256=str(split_binding["sha256"]),
            generation=str(binding_document["generation"]),
            span_manifest_digest=str(binding_document["span_manifest_digest"]),
            policy_digest=str(frozen["policy_digest"]),
            system_digest=system_digest,
        )
        # The holdout bytes and JSON are deliberately untouched until the
        # canonical reservation above is durable. Every failure below burns it.
        if split_binding["sha256"] != _file_sha256(gold_path):
            raise EvalError("gold split bytes do not match the gold manifest")
        suite = load_gold(gold_path, split)
        _validate_suite_identity(
            suite,
            gold_binding=binding_document,
            metadata=metadata,
        )
        _validate_gold_judgments(manifest, suite)
    else:
        frozen = None

    assert suite is not None

    responses, latencies, collection_metadata = _run_modes(
        suite=suite,
        manifest=manifest,
        runtime=runtime,
        collection=collection,
        embedding_snapshot=embedding_snapshot,
        reranker_snapshot=reranker_snapshot,
    )
    # A real evaluation cannot reach this point with an absent manifest:
    # `_manifest_metadata` has already opened it. The existence guard keeps
    # orchestration unit tests that replace that preflight independently useful.
    if manifest.exists() and _file_sha256(manifest) != manifest_sha:
        raise EvalError("span manifest changed during evaluation")
    if _catalog_release_binding(runtime.catalog, metadata) != catalog_binding:
        raise EvalError("current catalog changed during evaluation")
    for key, value in expected_collection.items():
        if collection_metadata.get(key) != value:
            raise EvalError(f"Qdrant collection metadata {key} mismatch")
    postflight_collection_contract = _preflight_collection(
        runtime=runtime,
        collection=collection,
        manifest=manifest,
        expected_metadata=expected_collection,
        expected_points=expected_points,
        expected_vector_set_sha256=migration_binding["vector_set_sha256"],
    )
    if postflight_collection_contract != collection_contract:
        raise EvalError("Qdrant collection changed during evaluation")

    metrics: dict[str, Any] = {}
    for mode in MODES:
        metrics[mode] = aggregate_metrics(
            suite.records,
            responses[mode],
            latencies[mode],
        )
    if split == "dev":
        selected = select_abstention_policy(
            suite.records,
            responses["hybrid-rerank"],
        )
        policy_core = {
            **freeze_policy(selected["policy"]),
            "system": system,
            "system_digest": system_digest,
            "development_gold_digest": suite.gold_digest,
            "development_selection_feasible": selected["feasible"],
        }
        frozen = policy_core
    assert frozen is not None
    final_responses = _apply_policy(
        responses["hybrid-rerank"],
        frozen["policy"],
    )
    final_metrics = aggregate_metrics(
        suite.records,
        final_responses,
        latencies["hybrid-rerank"],
    )
    gate = _gate(
        final_metrics,
        (metrics["vector"], metrics["lexical"]),
    )
    if (
        split == "dev"
        and gate["status"] == "passed"
        and frozen.get("development_selection_feasible") is not True
    ):
        raise EvalError("passed development gate has an infeasible policy")
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "split": split,
        "status": gate["status"],
        "evaluated_unix": time.time(),
        "gold": {
            "path": str(gold_path),
            "digest": suite.gold_digest,
            "records": len(suite.records),
            "binding_manifest": str(gold_manifest_path),
            "binding_manifest_digest": gold_binding["manifest_digest"],
        },
        "system": system,
        "system_digest": system_digest,
        "collection_metadata": collection_metadata,
        "collection_contract": collection_contract,
        "postflight_collection_contract": postflight_collection_contract,
        "policy": frozen,
        "metrics": metrics,
        "final_metrics": final_metrics,
        "gate": gate,
        "model_files": {
            "embedding": embedding_files,
            "reranker": reranker_files,
        },
        "responses": {
            mode: {
                query_id: _compact_response(response)
                for query_id, response in mode_responses.items()
            }
            for mode, mode_responses in responses.items()
        },
        "canonical_mutation": "disabled",
        "network_access": "loopback-only",
    }
    evidence_digest = _sha256_bytes(_canonical_json(evidence))
    evidence["evidence_digest"] = evidence_digest
    security.atomic_write_json(output_path, evidence, replace=False)
    if split == "dev" and gate["status"] == "passed":
        release_policy = {
            **frozen,
            "development_evidence": {
                "path": str(output_path),
                "file_sha256": _file_sha256(output_path),
                "evidence_digest": evidence_digest,
            },
        }
        security.atomic_write_json(policy_path, release_policy, replace=False)
    if split == "holdout":
        assert ledger_path is not None
        record_holdout_run(
            gold_digest=suite.gold_digest,
            gold_file_sha256=str(split_binding["sha256"]),
            generation=suite.generation,
            span_manifest_digest=suite.span_manifest_digest,
            policy_digest=str(frozen["policy_digest"]),
            system_digest=system_digest,
            evidence_digest=evidence_digest,
            evidence_file_sha256=_file_sha256(output_path),
        )
        validate_completed_holdout_ledger(
            gold_file_sha256=str(split_binding["sha256"]),
            generation=suite.generation,
            span_manifest_digest=suite.span_manifest_digest,
            policy_digest=str(frozen["policy_digest"]),
            system_digest=system_digest,
            gold_digest=suite.gold_digest,
            evidence_digest=evidence_digest,
            evidence_file_sha256=_file_sha256(output_path),
        )
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, choices=("dev", "holdout"))
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--gold-manifest", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embedding-snapshot", required=True, type=Path)
    parser.add_argument("--reranker-snapshot", required=True, type=Path)
    parser.add_argument("--migration-evidence", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    security.activate_private_umask()
    args = _parser().parse_args(argv)
    try:
        evidence = evaluate(
            split=args.split,
            gold_path=args.gold,
            gold_manifest_path=args.gold_manifest,
            manifest=args.manifest,
            collection=args.collection,
            embedding_snapshot=args.embedding_snapshot,
            reranker_snapshot=args.reranker_snapshot,
            migration_evidence_path=args.migration_evidence,
            policy_path=args.policy,
            output_path=args.output,
        )
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "split": evidence["split"],
                    "evidence_digest": evidence["evidence_digest"],
                    "gate": evidence["gate"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if evidence["status"] == "passed" else 3
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
