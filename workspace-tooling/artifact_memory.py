#!/usr/bin/env python3
"""Read-only access to workspace artifact search, sources, and temporal facts.

This is the local backend used by the platform MCP server. It never
mutates canonical sources, Qdrant, FalkorDB, receipts, outboxes, or checkpoint
state. Qdrant results are discovery hints; ``get`` re-verifies the canonical
catalog revision before returning source text. Graphiti pilot facts are hidden
from every read path until publication and approval controls exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import artifact_ingestion as ingestion
import artifact_runtime
import artifact_security as security
import artifact_service_client as service_client


SCHEMA_VERSION = 1
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_DERIVED_ROOT = artifact_runtime.derived_root()
DEFAULT_APPROVALS = DEFAULT_DERIVED_ROOT / "graphiti-approvals.json"
DEFAULT_RECEIPTS = DEFAULT_DERIVED_ROOT / "skill-events"
DEFAULT_CONSUMER_STATE = DEFAULT_DERIVED_ROOT / "artifact-event-consumer.sqlite3"
SNAPSHOT_HEALTH = DEFAULT_DERIVED_ROOT / "artifact-backup-snapshot-health.json"
OFFDEVICE_HEALTH = DEFAULT_DERIVED_ROOT / "artifact-backup-offdevice-health.json"
# Ratified 2026-07-18 -- see plans/artifact-memory-backup-dr-policy-2026-07-18.md.
RPO_HOURS = 24
RETENTION_WARN_DAYS = 14
BACKUP_AGENT_LABEL = "com.personal.artifact-backup-daily"
GROUP_ID = re.compile(r"^[A-Za-z0-9_-]+$")
HISTORICAL_SNIPPETS_DISABLED = (
    "historical artifact snippets are disabled until immutable CAS-backed "
    "revision retrieval exists"
)
PILOT_ACCESS_DISABLED = (
    "unapproved Graphiti pilot access is disabled until publication and "
    "approval controls exist"
)


class MemoryReadError(ValueError):
    """A read request is invalid, unsafe, or unavailable."""


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _workspace(raw: Path) -> Path:
    path = raw.expanduser().absolute()
    if path.is_symlink():
        raise MemoryReadError(f"workspace must not be a symlink: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise MemoryReadError(f"workspace is not a directory: {resolved}")
    return resolved


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_catalog_source(workspace: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise MemoryReadError(f"unsafe workspace-relative path: {relative!r}")
    lexical = Path(os.path.abspath(os.fspath(workspace / candidate)))
    if not _inside(lexical, workspace):
        raise MemoryReadError(f"source escapes workspace: {relative}")
    current = workspace
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise MemoryReadError(f"source path contains a symlink: {current}")
    return lexical


def _current_catalog_row(catalog: Path, relative: str) -> dict[str, Any]:
    database = catalog.expanduser().resolve(strict=True)
    security.require_private_file(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT revision_id, content_sha256, byte_size, mtime_ns, "
            "artifact_type, authority_class, project, repository "
            "FROM current_artifact_revisions WHERE relative_path=?",
            (relative,),
        ).fetchone()
    if row is None:
        raise MemoryReadError(
            f"source is not in the current default-deny artifact catalog: {relative}"
        )
    return dict(row)


def _read_verified(path: Path, expected: dict[str, Any], max_chars: int) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    content = bytearray()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise MemoryReadError(f"source is not a regular file: {path}")
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
        raise MemoryReadError(f"source changed while being read: {path}")
    actual = digest.hexdigest()
    if (
        actual != expected["content_sha256"]
        or after.st_size != int(expected["byte_size"])
        or after.st_mtime_ns != int(expected["mtime_ns"])
    ):
        raise MemoryReadError(
            "source no longer matches the current catalog revision; refresh the catalog"
        )
    text = bytes(content).decode("utf-8", errors="replace")
    return {
        "content": text[:max_chars],
        "content_chars": len(text),
        "returned_chars": min(len(text), max_chars),
        "truncated": len(text) > max_chars,
        "content_sha256": actual,
    }


def get_artifact(
    *,
    workspace: Path,
    catalog: Path,
    relative_path: str,
    max_chars: int,
) -> dict[str, Any]:
    if not 256 <= max_chars <= 50_000:
        raise MemoryReadError("max_chars must be between 256 and 50000")
    relative = Path(relative_path).as_posix()
    row = _current_catalog_row(catalog, relative)
    source = _safe_catalog_source(workspace, relative)
    verified = _read_verified(source, row, max_chars)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "canonical-source",
        "relative_path": relative,
        "absolute_path": str(source),
        "revision_id": row["revision_id"],
        "artifact_type": row["artifact_type"],
        "authority_class": row["authority_class"],
        "project": row["project"],
        "repository": row["repository"],
        "catalog_current": True,
        "source_mutation": "disabled",
        **verified,
    }


def _server_search(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Ask the resident service, the only reader of the serving store.

    Qdrant credentials are service-only and the embedded store is the retained
    rollback copy, so the socket is the operator's read path too. A search that
    cannot reach the service fails loudly: silently answering from the rollback
    store would return a different generation's data under the serving store's
    name, which is worse than no answer.
    """
    try:
        result = service_client.post_json(
            socket_path=runtime.service_socket_path,
            route="/v1/search",
            payload=payload,
            timeout=60.0,
        )
    except service_client.ArtifactServiceError as exc:
        raise MemoryReadError(
            f"the serving store is reachable only through the resident "
            f"service and the request failed ({type(exc).__name__}: {exc}); "
            f"the retained embedded store is the read-only rollback copy and "
            f"is deliberately not consulted"
        ) from exc
    # The service already stamps the envelope, and on the exact-hybrid path it
    # reports a stronger authority than this module can. Only fill what a
    # legacy service omitted rather than overwriting its answer.
    for key, value in (
        ("schema_version", SCHEMA_VERSION),
        ("authority", "discovery-only"),
        ("verification", "read returned relative_path with get_artifact"),
        ("source_mutation", "disabled"),
    ):
        result.setdefault(key, value)
    return result


def search_artifacts(
    *,
    workspace: Path,
    catalog: Path,
    query: str,
    limit: int,
    include_history: bool,
    project: str | None,
    artifact_type: str | None,
    authority_class: str | None,
    repository: str | None,
    lifecycle_hint: str | None,
    config_path: Path = artifact_runtime.DEFAULT_CONFIG,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise MemoryReadError("query must not be empty")
    if not 1 <= limit <= 25:
        raise MemoryReadError("limit must be between 1 and 25")
    if include_history:
        raise MemoryReadError(HISTORICAL_SNIPPETS_DISABLED)
    filters = {
        "project": project,
        "artifact_type": artifact_type,
        "authority_class": authority_class,
        "repository": repository,
        "lifecycle_hint": lifecycle_hint,
    }
    # Which store clients ride is the runtime configuration's call, not this
    # module's default. Opening the embedded path under a server-mode runtime
    # answers from the retained rollback copy and takes the local-mode lock on
    # a store the configuration declares read-only.
    runtime, _ = _runtime_selector(config_path)
    if runtime is not None and runtime.active_backend == "server":
        return _server_search(runtime, {"query": query, "limit": limit, **filters})
    result = ingestion.qdrant_search(
        workspace=workspace,
        catalog=catalog,
        local_path=ingestion.DEFAULT_QDRANT_PATH,
        url=None,
        api_key_env="QDRANT_API_KEY",
        collection=ingestion.DEFAULT_COLLECTION,
        embedding_model=ingestion.DEFAULT_EMBEDDING_MODEL,
        query=query,
        limit=limit,
        current_only=True,
        **filters,
    )
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "authority": "discovery-only",
            "verification": "read returned relative_path with get_artifact",
            "source_mutation": "disabled",
        }
    )
    return result


def _approved_groups(path: Path) -> set[str]:
    if not path.exists():
        return set()
    if path.is_symlink() or not path.is_file():
        raise MemoryReadError(f"unsafe Graphiti approval registry: {path}")
    security.require_private_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise MemoryReadError("unsupported Graphiti approval registry schema")
    values = payload.get("approved_groups")
    if not isinstance(values, list) or not all(
        isinstance(value, str) and GROUP_ID.fullmatch(value) for value in values
    ):
        raise MemoryReadError("Graphiti approved_groups must be a group-id list")
    return set(values)


def temporal_facts(
    *,
    query: str,
    limit: int,
    group_id: str | None,
    include_pilot: bool,
    approvals: Path,
    host: str,
    port: int,
    password_env: str,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise MemoryReadError("query must not be empty")
    if not 1 <= limit <= 25:
        raise MemoryReadError("limit must be between 1 and 25")
    if include_pilot:
        raise MemoryReadError(PILOT_ACCESS_DISABLED)
    if group_id is not None and not GROUP_ID.fullmatch(group_id):
        raise MemoryReadError("group_id contains unsupported characters")
    approved = _approved_groups(approvals)
    if group_id is not None and group_id not in approved:
        raise MemoryReadError("Graphiti group is not approved or unavailable")
    if not approved:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "temporal-fact-read",
            "query": query,
            "include_pilot": False,
            "pilot_access": "disabled",
            "approved_groups": [],
            "results": [],
            "authority": "approved-temporal-facts",
            "graph_mutation": "disabled",
        }
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise MemoryReadError("FalkorDB client is unavailable") from exc
    client = FalkorDB(
        host=host,
        port=port,
        password=os.environ.get(password_env),
    )
    facts: list[dict[str, Any]] = []
    try:
        available = {str(value) for value in client.list_graphs()}
        selected = [group_id] if group_id is not None else sorted(approved)
        for name in selected:
            if name not in available:
                raise MemoryReadError("Graphiti group is not approved or unavailable")
            remaining = limit - len(facts)
            if remaining <= 0:
                break
            rows = client.select_graph(name).query(
                "MATCH (a:Entity)-[r]->(b:Entity) "
                "WHERE toLower(a.name) CONTAINS $needle "
                "OR toLower(b.name) CONTAINS $needle "
                "OR toLower(r.name) CONTAINS $needle "
                "OR toLower(r.fact) CONTAINS $needle "
                "RETURN a.name, r.name, b.name, r.fact, r.valid_at, "
                "r.invalid_at, r.episodes "
                f"ORDER BY r.valid_at DESC LIMIT {remaining}",
                params={"needle": query.casefold()},
            ).result_set
            facts.extend(
                {
                    "group_id": name,
                    "quality_status": "approved",
                    "source_entity": row[0],
                    "relation": row[1],
                    "target_entity": row[2],
                    "fact": row[3],
                    "valid_at": row[4],
                    "invalid_at": row[5],
                    "episode_ids": row[6],
                }
                for row in rows
            )
    finally:
        client.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "temporal-fact-read",
        "query": query,
        "include_pilot": False,
        "pilot_access": "disabled",
        "approved_groups": sorted(approved),
        "results": facts[:limit],
        "authority": "approved-temporal-facts",
        "graph_mutation": "disabled",
    }


def _catalog_status(catalog: Path) -> dict[str, Any]:
    database = catalog.expanduser().resolve(strict=True)
    security.require_private_file(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        current = connection.execute(
            "SELECT run_id, finished_at FROM scan_runs "
            "WHERE finished_at IS NOT NULL AND status='complete' "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        latest = connection.execute(
            "SELECT run_id, finished_at, status FROM scan_runs "
            "WHERE finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        artifacts = int(
            connection.execute(
                "SELECT COUNT(*) FROM current_artifact_revisions"
            ).fetchone()[0]
        )
    return {
        "available": current is not None,
        "run_id": int(current[0]) if current else None,
        "finished_at": str(current[1]) if current else None,
        "latest_attempt_run_id": int(latest[0]) if latest else None,
        "latest_attempt_status": str(latest[2]) if latest else None,
        "current_artifacts": artifacts,
    }


def _runtime_selector(config_path: Path) -> tuple[Any | None, dict[str, Any]]:
    """Read the backend selector without opening any vector store.

    A status command must describe the store clients actually ride. The
    runtime configuration is the only authority on which store that is, so it
    is consulted before anything is opened.
    """
    try:
        runtime = artifact_runtime.load_runtime(config_path)
    except (
        artifact_runtime.RuntimeConfigError,
        security.PrivateStateError,
        OSError,
    ) as exc:
        return None, {
            "available": False,
            "config": str(config_path),
            "active_backend": "unknown",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "note": (
                "runtime configuration is unreadable; falling back to the "
                "embedded store as the pre-migration layout"
            ),
        }
    return runtime, {
        "available": True,
        "config": str(runtime.config_path),
        "active_backend": runtime.active_backend,
        "active_retrieval": runtime.active_retrieval,
        "generation": runtime.qdrant_generation,
        "collection": runtime.qdrant_collection,
        "service_socket": str(runtime.service_socket_path),
    }


def _server_store_status(runtime: Any) -> dict[str, Any]:
    """Report the serving store as the resident service reports it.

    Qdrant credentials are service-only, so the socket is the operator's read
    path too — asking it keeps this command and the clients on one answer.
    """
    summary: dict[str, Any] = {
        "role": "active",
        "backend": "server",
        "source": "resident-service-socket",
        "collection": runtime.qdrant_collection,
        "generation": runtime.qdrant_generation,
    }
    try:
        payload = service_client.post_json(
            socket_path=runtime.service_socket_path,
            route="/v1/status",
            payload={},
            timeout=15.0,
        )
    except service_client.ArtifactServiceError as exc:
        summary.update(
            {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "note": (
                    "the serving store holds the only current data and is "
                    "reachable only through the resident service"
                ),
            }
        )
        return summary
    qdrant = payload.get("qdrant")
    service = payload.get("service")
    qdrant = qdrant if isinstance(qdrant, dict) else {}
    service = service if isinstance(service, dict) else {}
    summary.update(
        {
            "available": qdrant.get("available") is True,
            "collection": qdrant.get("collection", runtime.qdrant_collection),
            "generation": qdrant.get("generation", runtime.qdrant_generation),
            "points": qdrant.get("points"),
            "current_points": qdrant.get("current_points"),
            "historical_points": qdrant.get("historical_points"),
            "snapshot_age_seconds": qdrant.get("snapshot_age_seconds"),
            "active_retrieval": service.get("active_retrieval"),
            "service_status": service.get("status"),
            "service_degraded_reasons": service.get("degraded_reasons"),
            "error": qdrant.get("error"),
        }
    )
    return summary


def _open_embedded_store(path: Path, collection: str) -> dict[str, Any]:
    """Open the embedded store. Only ever called when explicitly requested."""
    try:
        from qdrant_client import QdrantClient

        security.require_private_directory(path)
        client = QdrantClient(path=str(path))
        try:
            exists = client.collection_exists(collection)
            points = (
                int(
                    client.count(
                        collection_name=collection,
                        exact=True,
                    ).count
                )
                if exists
                else 0
            )
            return {
                "available": True,
                "opened": True,
                "collection": collection,
                "points": points,
            }
        finally:
            client.close()
    except Exception as exc:
        return {
            "available": False,
            "opened": True,
            "collection": collection,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def _retention_expired(retain_until: str) -> bool | None:
    """Report whether the rollback window has already passed by calendar."""
    try:
        deadline = datetime.fromisoformat(retain_until.replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


def _embedded_store_status(
    runtime: Any | None,
    *,
    open_store: bool,
) -> dict[str, Any]:
    """Describe the embedded store, opening it only on explicit request.

    Under the server backend the embedded store is the retained rollback copy.
    Opening it would contend for the same local-mode lock the resident service
    and consumer would take, so it is described from configuration by default.
    """
    if runtime is None:
        path = ingestion.DEFAULT_QDRANT_PATH
        collection = ingestion.DEFAULT_COLLECTION
        summary: dict[str, Any] = {
            "role": "active",
            "backend": "embedded",
            "path": str(path),
            "generation": None,
            "label": "active (runtime configuration unavailable)",
        }
        summary.update(_open_embedded_store(path, collection))
        return summary

    path = runtime.embedded_path
    collection = ingestion.DEFAULT_COLLECTION
    rollback = runtime.active_backend != "embedded"
    summary = {
        "role": "rollback" if rollback else "active",
        "backend": "embedded",
        "path": str(path),
        "generation": None,
        "mode": runtime.rollback_mode if rollback else "read-write",
        "retained_until": runtime.rollback_until,
        "retention_expired": _retention_expired(runtime.rollback_until),
        "label": (
            f"rollback ({runtime.rollback_mode}, retained until "
            f"{runtime.rollback_until})"
            if rollback
            else "active"
        ),
    }
    if not rollback or open_store:
        summary.update(_open_embedded_store(path, collection))
        return summary
    summary.update(
        {
            "available": None,
            "opened": False,
            "collection": collection,
            "note": (
                "not opened; this is the retained rollback copy and its counts "
                "diverge from the serving store. Pass --rollback-store to "
                "inspect it"
            ),
        }
    )
    return summary


def _graphiti_status(host: str, port: int, password_env: str, approvals: Path) -> dict[str, Any]:
    try:
        approved = _approved_groups(approvals)
        if not approved:
            return {
                "available": None,
                "status": "not-probed-no-approved-groups",
                "groups": [],
                "pilot_access": "disabled",
            }
        from falkordb import FalkorDB

        client = FalkorDB(
            host=host,
            port=port,
            password=os.environ.get(password_env),
        )
        try:
            groups = {str(value) for value in client.list_graphs()}
            counts = []
            for name in sorted(approved):
                if name not in groups:
                    continue
                graph = client.select_graph(name)
                episodes = int(
                    graph.query("MATCH (e:Episodic) RETURN count(e)").result_set[0][0]
                )
                counts.append(
                    {
                        "group_id": name,
                        "episodes": episodes,
                        "quality_status": "approved",
                    }
                )
            return {
                "available": True,
                "groups": counts,
                "pilot_access": "disabled",
            }
        finally:
            client.close()
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def _receipt_count(root: Path) -> int:
    if not root.exists():
        return 0
    security.require_private_directory(root)
    count = 0
    for path in root.glob("[0-9a-f][0-9a-f]/*.json"):
        if path.is_file():
            security.require_private_file(path)
            count += 1
    return count


def _consumer_status(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        security.require_private_file(path)
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM consumer_events GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}
    except sqlite3.Error:
        return {"unavailable": 1}


def _path_divergence(
    runtime: Any | None,
    inspected: dict[str, Path],
) -> list[dict[str, str]]:
    """Name every path this command read that the runtime does not declare."""
    if runtime is None:
        return []
    declared = {
        "catalog": runtime.catalog,
        "receipt_root": runtime.receipt_root,
        "consumer_state": runtime.consumer_state,
    }
    divergent = []
    for name, used in inspected.items():
        expected = declared.get(name)
        if expected is None:
            continue
        if Path(os.path.abspath(os.fspath(used))) != Path(
            os.path.abspath(os.fspath(expected))
        ):
            divergent.append(
                {
                    "field": name,
                    "inspected": str(used),
                    "runtime_declares": str(expected),
                }
            )
    return divergent


def _read_health(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        security.require_private_file(path)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age_hours(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round(
        (datetime.now(timezone.utc) - moment).total_seconds() / 3600,
        2,
    )


def _retention_status(runtime: Any | None) -> dict[str, Any]:
    """Surface the embedded-store retention deadline and backup freshness.

    The runtime config has always carried `rollback.retain_embedded_until` and
    `artifact_runtime` has always parsed it into `rollback_until` -- but nothing
    ever displayed it, so the window could expire by calendar with no operator
    signal (gap-analysis §13, F-04 item 4). This makes it visible.

    Backup freshness is reported next to it because both answer the same
    question: what is about to be lost, and when.
    """
    status: dict[str, Any] = {"available": runtime is not None}
    if runtime is None:
        status["error"] = "runtime configuration unavailable"
        return status

    status["embedded_retention"] = {"retain_until": runtime.rollback_until}
    try:
        deadline = datetime.fromisoformat(runtime.rollback_until)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds() / 86400
        status["embedded_retention"]["days_remaining"] = round(remaining, 1)
        if remaining <= 0:
            level = "expired"
        elif remaining <= RETENTION_WARN_DAYS:
            level = "expiring-soon"
        else:
            level = "ok"
        status["embedded_retention"]["level"] = level
        status["embedded_retention"]["action"] = (
            "decide keep-or-delete; deletion needs separate approval (ADR-002 §7)"
            if level != "ok"
            else "no action yet"
        )
    except ValueError:
        status["embedded_retention"]["level"] = "unparseable"

    snapshot = _read_health(SNAPSHOT_HEALTH)
    newest = (snapshot or {}).get("newest_snapshot") or {}
    status["snapshot"] = {
        "cadence_scheduled": _launch_agent_loaded(BACKUP_AGENT_LABEL),
        "newest_age_hours": newest.get("age_hours"),
        "last_run": (snapshot or {}).get("ran_at"),
        "last_run_healthy": (snapshot or {}).get("healthy"),
        "retention_level": ((snapshot or {}).get("retention") or {}).get("level"),
    }

    offdevice = _read_health(OFFDEVICE_HEALTH)
    # `completed_at` is the last COMPLETE backup (the full hard-required tier-1 +
    # tier-2 inventory -- `bundle_complete`): an incomplete run no longer advances
    # it, so RPO freshness reflects real recoverability. The latest run and its
    # completeness are surfaced alongside so an incomplete run is visible even when
    # the last complete backup is still within RPO.
    offdevice_health = offdevice or {}
    age = _age_hours(offdevice_health.get("completed_at"))
    tier1_complete = offdevice_health.get("tier1_complete")
    # Prefer the full tier-1+tier-2 completeness predicate; a pre-M4 watermark
    # (written before `bundle_complete` existed) falls back to `tier1_complete`.
    # Keying `last_run_incomplete` on tier1_complete alone would report a
    # tier-2-only gap (Sol #3) as healthy on the dashboard for up to a full RPO
    # window, which is exactly the honesty hole this milestone closes.
    bundle_complete = offdevice_health.get("bundle_complete")
    complete = bundle_complete if bundle_complete is not None else tier1_complete
    status["off_device"] = {
        "last_backup": offdevice_health.get("completed_at"),
        "last_complete_backup": offdevice_health.get("completed_at"),
        "last_run": offdevice_health.get("last_run_at"),
        "age_hours": age,
        "rpo_hours": RPO_HOURS,
        "rpo_status": (
            "NEVER-BACKED-UP"
            if age is None
            else ("breach" if age > RPO_HOURS else "within")
        ),
        "tier1_complete": tier1_complete,
        "bundle_complete": bundle_complete,
        "absent_items": offdevice_health.get("absent_items") or [],
        "incomplete_items": offdevice_health.get("incomplete_items") or [],
        "last_run_incomplete": complete is False,
        "rpo_ratified": True,
        # The bundle is encrypted and rotated, but it still lives on the same
        # volume as everything it protects. That covers corruption and operator
        # error; it does NOT cover losing the disk. Reported separately so the
        # two are never confused.
        "same_volume_as_source": _same_volume_as_source(offdevice),
        "whole_system_restore_rehearsed": False,
    }
    return status


def _same_volume_as_source(offdevice: dict[str, Any] | None) -> bool | None:
    """True when the newest bundle sits on the same volume it is protecting.

    A backup on the source volume is not disaster recovery, and the difference
    is invisible in a path string -- so it is measured, by device id.
    """
    destination = (offdevice or {}).get("destination")
    if not destination:
        return None
    try:
        return (
            os.stat(destination).st_dev == os.stat(DEFAULT_DERIVED_ROOT).st_dev
        )
    except OSError:
        return None


def _launch_agent_loaded(label: str) -> bool:
    """Report whether the snapshot cadence agent is actually loaded.

    A written-but-unbootstrapped plist is the F-02 shape: the file exists, the
    dashboard looks configured, and nothing has ever run. Status must
    distinguish the two.
    """
    try:
        result = subprocess.run(
            ["/bin/launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(label in line for line in result.stdout.splitlines())


def memory_status(
    *,
    catalog: Path,
    approvals: Path,
    receipt_root: Path,
    consumer_state: Path,
    host: str,
    port: int,
    password_env: str,
    config_path: Path = artifact_runtime.DEFAULT_CONFIG,
    rollback_store: bool = False,
) -> dict[str, Any]:
    runtime, selector = _runtime_selector(config_path)
    if runtime is not None and runtime.active_backend == "server":
        active = _server_store_status(runtime)
        embedded = _embedded_store_status(runtime, open_store=rollback_store)
    else:
        active = _embedded_store_status(runtime, open_store=True)
        embedded = active
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "read-only-status",
        "runtime": selector,
        "catalog": _catalog_status(catalog),
        "qdrant": active,
        "embedded_store": embedded,
        "retention_and_backup": _retention_status(runtime),
        "graphiti": _graphiti_status(
            host,
            port,
            password_env,
            approvals,
        ),
        "skill_receipts": {
            "count": _receipt_count(receipt_root),
            "consumer_status": _consumer_status(consumer_state),
        },
        "path_divergence": _path_divergence(
            runtime,
            {
                "catalog": catalog,
                "receipt_root": receipt_root,
                "consumer_state": consumer_state,
            },
        ),
        "read_controls": {
            "historical_snippets": "disabled",
            "unapproved_graphiti_pilots": "disabled",
        },
        "source_mutation": "disabled",
        "sink_mutation": "disabled",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--catalog", type=Path, default=ingestion.DEFAULT_CATALOG)
    parser.add_argument("--approvals", type=Path, default=DEFAULT_APPROVALS)
    parser.add_argument(
        "--config",
        type=Path,
        default=artifact_runtime.DEFAULT_CONFIG,
        help="runtime configuration declaring the active backend",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--password-env", default="FALKORDB_PASSWORD")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="semantic Qdrant search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--project")
    search.add_argument("--artifact-type")
    search.add_argument("--authority-class")
    search.add_argument("--repository")
    search.add_argument("--lifecycle-hint")
    search.add_argument(
        "--include-history",
        action="store_true",
        help="disabled until immutable CAS-backed revision retrieval exists",
    )

    get = commands.add_parser("get", help="read one verified canonical artifact")
    get.add_argument("--relative-path", required=True)
    get.add_argument("--max-chars", type=int, default=12_000)

    facts = commands.add_parser("facts", help="read approved temporal facts")
    facts.add_argument("--query", required=True)
    facts.add_argument("--limit", type=int, default=8)
    facts.add_argument("--group-id")
    facts.add_argument(
        "--include-pilot",
        action="store_true",
        help="disabled until publication and approval controls exist",
    )

    status = commands.add_parser(
        "status", help="summarize local artifact-memory state"
    )
    status.add_argument(
        "--rollback-store",
        action="store_true",
        help=(
            "also open the retained embedded rollback store; it contends for "
            "the local-mode lock and its counts are not what clients read"
        ),
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = _workspace(args.workspace)
    if args.command == "search":
        return search_artifacts(
            workspace=workspace,
            catalog=args.catalog,
            query=args.query,
            limit=args.limit,
            include_history=args.include_history,
            project=args.project,
            artifact_type=args.artifact_type,
            authority_class=args.authority_class,
            repository=args.repository,
            lifecycle_hint=args.lifecycle_hint,
            config_path=args.config,
        )
    if args.command == "get":
        return get_artifact(
            workspace=workspace,
            catalog=args.catalog,
            relative_path=args.relative_path,
            max_chars=args.max_chars,
        )
    if args.command == "facts":
        return temporal_facts(
            query=args.query,
            limit=args.limit,
            group_id=args.group_id,
            include_pilot=args.include_pilot,
            approvals=args.approvals,
            host=args.host,
            port=args.port,
            password_env=args.password_env,
        )
    if args.command == "status":
        return memory_status(
            catalog=args.catalog,
            approvals=args.approvals,
            receipt_root=DEFAULT_RECEIPTS,
            consumer_state=DEFAULT_CONSUMER_STATE,
            host=args.host,
            port=args.port,
            password_env=args.password_env,
            config_path=args.config,
            rollback_store=args.rollback_store,
        )
    raise MemoryReadError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (
        MemoryReadError,
        ingestion.IngestionError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            ),
            file=sys.stderr,
        )
        return 2
    _json_print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
